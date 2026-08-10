"""Integration tests for config-path resolution from linked worktrees.

When invoked from a task worktree, `tusk config` reads the **primary checkout's**
config (the deliberate shared-config invariant). Operators editing
`tusk/config.json` in a worktree previously had no way to tell which file was
being read. The fix prints `Config: <resolved-path>` to stderr on every
invocation, keeping the diagnostic out of JSON / value consumers' stdout pipes.

`tusk validate` instead validates a worktree-local config when one exists so
the command checks the file that a task is about to commit, while retaining the
primary checkout's shared database for trigger-drift validation (issue #1288).
"""

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _git(args, *, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _tusk_init(repo):
    env = os.environ.copy()
    env["TUSK_DB"] = str(repo / "tusk" / "tasks.db")
    env["TUSK_QUIET"] = "1"
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return env


def _run_tusk(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_config_emits_resolved_path_to_stderr(tmp_path):
    """`tusk config` (no key) prints `Config: <path>` to stderr and clean JSON to stdout."""
    repo = _init_repo(tmp_path)
    env = _tusk_init(repo)

    result = _run_tusk(["config"], cwd=repo, env=env)

    assert result.returncode == 0, (
        f"tusk config failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    # Stderr must contain the diagnostic line.
    assert "Config:" in result.stderr, (
        f"expected 'Config:' in stderr; got: {result.stderr!r}"
    )
    expected_path = str(repo / "tusk" / "config.json")
    assert expected_path in result.stderr, (
        f"expected {expected_path!r} in stderr; got: {result.stderr!r}"
    )
    # Stdout must still parse as JSON — no contamination from the stderr line.
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    # And it should look like a real config (sanity).
    assert "domains" in payload


def test_config_with_key_emits_path_to_stderr_and_value_to_stdout(tmp_path):
    """`tusk config <key>` keeps stdout to the value alone; the path goes to stderr.

    `read_config_key` emits one value per line for array-typed keys; the
    diagnostic line must NOT contaminate stdout (it goes to stderr).
    """
    repo = _init_repo(tmp_path)
    env = _tusk_init(repo)

    # `task_types` is populated in config.default.json (`domains` defaults to []).
    result = _run_tusk(["config", "task_types"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert "Config:" in result.stderr
    # stdout must contain newline-separated task_type values and NOT contain the
    # diagnostic line — that's the contract: stderr is invisible to stdout pipes.
    stdout_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(stdout_lines) > 0, "expected at least one task_type on stdout"
    assert not any("Config:" in l for l in stdout_lines), (
        f"diagnostic line leaked into stdout; got: {stdout_lines!r}"
    )


def test_config_stderr_path_matches_resolve_config_from_worktree(tmp_path):
    """From a linked worktree, the echoed path must be the primary checkout's
    config (the deliberate shared-config invariant) — NOT the worktree's path.
    This is the issue #767 scenario the diagnostic was added to disambiguate.
    """
    repo = _init_repo(tmp_path)
    env = _tusk_init(repo)
    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/config-test"], cwd=repo)

    # Plant a divergent config in the worktree to simulate the operator's
    # branch-local edit — `tusk config` must NOT read this one.
    worktree_cfg_dir = worktree / "tusk"
    worktree_cfg_dir.mkdir(parents=True, exist_ok=True)
    (worktree_cfg_dir / "config.json").write_text(
        '{"domains": ["WORKTREE_LOCAL_MARKER"]}', encoding="utf-8"
    )

    result = _run_tusk(["config"], cwd=worktree, env=env)

    assert result.returncode == 0, result.stderr
    # Stderr names the PRIMARY's path, not the worktree's.
    primary_cfg = str(repo / "tusk" / "config.json")
    assert primary_cfg in result.stderr, (
        f"expected primary config path {primary_cfg!r} in stderr; got: {result.stderr!r}"
    )
    assert str(worktree_cfg_dir / "config.json") not in result.stderr, (
        "stderr must NOT name the worktree's config — the shared-config "
        "invariant means tusk config reads the primary's file"
    )
    # And stdout's payload must be the primary's config, not the worktree's.
    payload = json.loads(result.stdout)
    assert "WORKTREE_LOCAL_MARKER" not in payload.get("domains", []), (
        "stdout payload must come from the primary's config, not the worktree's"
    )


def test_validate_uses_worktree_config_with_primary_shared_database(tmp_path):
    repo = _init_repo(tmp_path)
    env = _tusk_init(repo)
    env.pop("TUSK_DB", None)
    env.pop("TUSK_PROJECT", None)

    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/validate-test"], cwd=repo)

    primary_config = repo / "tusk" / "config.json"
    worktree_config = worktree / "tusk" / "config.json"
    worktree_config.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(primary_config.read_text(encoding="utf-8"))
    config["domains"] = ["WORKTREE_LOCAL_MARKER"]
    worktree_config.write_text(json.dumps(config), encoding="utf-8")

    worktree_result = _run_tusk(["validate"], cwd=worktree, env=env)

    assert worktree_result.returncode == 1, worktree_result.stderr
    assert f"Config is valid ({worktree_config})." in worktree_result.stdout
    assert str(primary_config) not in worktree_result.stdout
    shared_db = repo / "tusk" / "tasks.db"
    assert f"Trigger drift detected ({shared_db}):" in worktree_result.stderr
    assert "missing trigger: validate_domain_insert" in worktree_result.stderr
    assert "missing trigger: validate_domain_update" in worktree_result.stderr
    assert str(worktree / "tusk" / "tasks.db") not in worktree_result.stderr

    primary_result = _run_tusk(["validate"], cwd=repo, env=env)

    assert primary_result.returncode == 0, primary_result.stderr
    assert f"Config is valid ({primary_config})." in primary_result.stdout
    assert "Validation triggers OK." in primary_result.stdout
