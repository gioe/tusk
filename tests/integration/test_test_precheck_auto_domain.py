"""Integration tests for ``tusk test-precheck`` auto-detecting the active
task's domain from the current branch (TASK-418, GitHub issue #836).

Before this change, ``tusk test-precheck`` only honored
``config.domain_test_commands[domain]`` when ``--domain`` was passed
explicitly.  The equivalent flow in ``tusk commit`` already auto-detects via
``load_task_domain`` (bin/tusk-commit.py:1098), so a scraper-domain task
whose ``tusk commit`` resolved to ``cd apps/scraper && pytest`` would see
``tusk test-precheck`` fall through to the multi-app global ``test_command``
— the bug that motivated the issue.

Coverage:
- Auto-detection: on a ``feature/TASK-<id>-<slug>`` branch, no ``--domain``,
  precheck resolves the task's ``domain_test_commands`` entry.
- Explicit override: ``--domain`` skips auto-detection.
- Fallback: no matching entry → global ``test_command``.
- Default branch: ``branch-parse`` fails → global ``test_command``.
- Agreement with ``tusk commit``: both resolvers return the same command for
  the same in-progress task / domain.
"""

import importlib.util
import json
import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", check=check,
    )


def _make_repo(tmp_path):
    """Create a git repo with tusk init, a global ``test_command`` of
    ``false``, and a ``cli`` domain command of ``true``.  ``cli`` is one of
    the default ``config.default.json`` domains, so task-insert validation
    accepts it without a triggers regen.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True,
    )
    _git("config", "user.email", "t@t", cwd=str(repo))
    _git("config", "user.name", "t", cwd=str(repo))
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=str(repo))
    _git("commit", "-q", "-m", "seed", cwd=str(repo))

    env = {**os.environ, "TUSK_PROJECT": str(repo), "TUSK_QUIET": "1"}
    # Drop any leaked TUSK_DB so tusk init writes to the in-repo path.
    env.pop("TUSK_DB", None)
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=str(repo), env=env, capture_output=True,
        text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"tusk init failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    cfg_path = repo / "tusk" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["test_command"] = "false"
    cfg["domain_test_commands"] = {"cli": "true"}
    cfg_path.write_text(json.dumps(cfg))

    return str(repo), env


def _insert_task(repo, env, *, domain=None):
    args = [TUSK_BIN, "task-insert", "test task", "desc",
            "--criteria", "ac"]
    if domain:
        args.extend(["--domain", domain])
    result = subprocess.run(
        args, cwd=repo, env=env, capture_output=True,
        text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"task-insert failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return json.loads(result.stdout)["task_id"]


def _precheck(repo, env, *args):
    return subprocess.run(
        [TUSK_BIN, "test-precheck", *args], cwd=repo, env=env,
        capture_output=True, text=True, encoding="utf-8",
    )


class TestAutoDetectFromCurrentBranch:
    def test_resolves_domain_command_on_feature_branch(self, tmp_path):
        repo, env = _make_repo(tmp_path)
        task_id = _insert_task(repo, env, domain="cli")
        _git("checkout", "-q", "-b", f"feature/TASK-{task_id}-x", cwd=repo)

        result = _precheck(repo, env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["test_command"] == "true", (
            f"expected the cli-domain command 'true', got "
            f"{payload['test_command']!r}; stderr={result.stderr!r}"
        )

    def test_explicit_domain_overrides_auto_detect(self, tmp_path):
        repo, env = _make_repo(tmp_path)
        task_id = _insert_task(repo, env, domain="cli")
        _git("checkout", "-q", "-b", f"feature/TASK-{task_id}-x", cwd=repo)

        # --domain skills has no entry in domain_test_commands → falls
        # through to global ``false``.  If auto-detect leaked through, this
        # would still resolve to 'true' (the cli entry), so a 'false' result
        # is the proof that the explicit value took precedence.
        result = _precheck(repo, env, "--domain", "skills")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["test_command"] == "false", (
            f"explicit --domain skills (no entry) must fall through to "
            f"global, got {payload['test_command']!r}"
        )

    def test_default_branch_falls_back_to_global(self, tmp_path):
        repo, env = _make_repo(tmp_path)
        _insert_task(repo, env, domain="cli")
        # Stay on main — branch-parse exits 1, auto-detect short-circuits.

        result = _precheck(repo, env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["test_command"] == "false"

    def test_branch_for_task_without_domain_falls_back_to_global(self, tmp_path):
        repo, env = _make_repo(tmp_path)
        task_id = _insert_task(repo, env)  # no --domain
        _git("checkout", "-q", "-b", f"feature/TASK-{task_id}-x", cwd=repo)

        result = _precheck(repo, env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["test_command"] == "false"

    def test_branch_for_domain_without_command_falls_back_to_global(self, tmp_path):
        # Task has a valid domain, but no domain_test_commands entry for it.
        repo, env = _make_repo(tmp_path)
        task_id = _insert_task(repo, env, domain="skills")
        _git("checkout", "-q", "-b", f"feature/TASK-{task_id}-x", cwd=repo)

        result = _precheck(repo, env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["test_command"] == "false"


class TestAgreementWithTuskCommit:
    """The original bug shape: precheck and commit disagreed on the resolved
    command for the same in-progress task.  These tests pin both resolvers
    to the same inputs and assert they return the same string."""

    def test_precheck_matches_commit_for_domain_task(self, tmp_path):
        repo, env = _make_repo(tmp_path)
        task_id = _insert_task(repo, env, domain="cli")
        _git("checkout", "-q", "-b", f"feature/TASK-{task_id}-x", cwd=repo)

        # Precheck's end-to-end resolved command.
        precheck_result = _precheck(repo, env)
        assert precheck_result.returncode == 0, precheck_result.stderr
        precheck_cmd = json.loads(precheck_result.stdout)["test_command"]

        # Commit's resolved command — load the helper directly so we don't
        # have to manufacture a real commit just to read what command would
        # have run.  ``load_test_command`` is the same function ``tusk
        # commit`` calls at bin/tusk-commit.py:1101 with the task's domain
        # already resolved via ``load_task_domain``.
        commit_script = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
        spec = importlib.util.spec_from_file_location(
            "tusk_commit", commit_script,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg_path = os.path.join(repo, "tusk", "config.json")
        commit_cmd = mod.load_test_command(cfg_path, "cli")

        assert precheck_cmd == commit_cmd == "true", (
            f"precheck and commit must resolve to the same domain command; "
            f"precheck={precheck_cmd!r}, commit={commit_cmd!r}"
        )
