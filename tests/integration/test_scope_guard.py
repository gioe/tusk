"""Integration tests for the pre-commit scope-guard hook (TASK-469).

The guard reads the current branch -> task id -> task_referenced_paths and
rejects commits whose staged paths fall outside (scope union always_allowed).

Each test exercises the guard via the .git/hooks/pre-commit dispatcher that
install.sh writes, so the wiring is part of the assertion.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")


def _run(cmd, cwd, check=True, env=None, input_bytes=None):
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        input=input_bytes,
    )
    if check:
        assert result.returncode == 0, (
            f"command {cmd} failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _git(args, cwd, check=True, env=None):
    return _run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", *args],
        cwd,
        check=check,
        env=env,
    )


@pytest.fixture()
def codex_sandbox(tmp_path):
    """A codex-layout git repo with tusk installed via install.sh."""
    _run(["git", "init"], tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    _run(["bash", INSTALL_SH], tmp_path)
    return tmp_path


def _sandbox_env(sandbox):
    env = os.environ.copy()
    env["PATH"] = str(sandbox / "tusk" / "bin") + os.pathsep + env.get("PATH", "")
    # Pin tusk to the sandbox project so subprocess invocations from inside
    # subshells (e.g. dispatcher -> guard -> tusk) don't drift to another repo.
    env["TUSK_PROJECT"] = str(sandbox)
    return env


def _seed_task(sandbox, summary, description):
    """Insert a task and return its integer ID."""
    env = _sandbox_env(sandbox)
    result = _run(
        ["tusk", "task-insert", summary, description, "--criteria", "seed"],
        sandbox,
        env=env,
    )
    import json as _json
    payload = _json.loads(result.stdout)
    return int(payload["task_id"])


def _invoke_pre_commit(sandbox, env=None):
    env = env or _sandbox_env(sandbox)
    return subprocess.run(
        [str(sandbox / ".git" / "hooks" / "pre-commit")],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


# ── 2167: rejects out-of-scope staged paths ─────────────────────────────


def test_rejects_out_of_scope(codex_sandbox):
    """A staged path outside (scope ∪ always_allowed) trips the guard."""
    # Task whose description references in/scope.txt -> scope = {"in/scope.txt"}
    task_id = _seed_task(
        codex_sandbox,
        "Touch in/scope.txt only",
        "This task only modifies the file at in/scope.txt and nothing else.",
    )
    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)

    (codex_sandbox / "out_of_scope.txt").write_text("nope\n")
    _git(["add", "out_of_scope.txt"], codex_sandbox)

    result = _invoke_pre_commit(codex_sandbox)
    assert result.returncode != 0, (
        f"scope-guard should reject out-of-scope path: stderr={result.stderr!r}"
    )
    assert "scope-guard rejected" in result.stderr
    assert "out_of_scope.txt" in result.stderr


# ── 2168: allows in-scope (or always-allowed) staged paths ──────────────


def test_allows_in_scope(codex_sandbox):
    """Staged paths inside the task scope, or in always_allowed, pass."""
    task_id = _seed_task(
        codex_sandbox,
        "Touch in/scope.txt only",
        "This task only modifies the file at in/scope.txt and nothing else.",
    )
    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)

    in_scope = codex_sandbox / "in" / "scope.txt"
    in_scope.parent.mkdir(parents=True, exist_ok=True)
    in_scope.write_text("real change\n")
    # VERSION lives in always_allowed by default
    (codex_sandbox / "VERSION").write_text("1\n")
    _git(["add", "in/scope.txt", "VERSION"], codex_sandbox)

    result = _invoke_pre_commit(codex_sandbox)
    assert result.returncode == 0, (
        f"scope-guard should accept in-scope + always-allowed paths: stderr={result.stderr!r}"
    )


# ── 2169: silent pass when current branch has no task id ────────────────


def test_silent_pass_no_task_id(codex_sandbox):
    """A branch that doesn't match feature/TASK-<id>-<slug> is unenforced."""
    _git(["checkout", "-b", "random-branch"], codex_sandbox)
    (codex_sandbox / "anything.txt").write_text("anything\n")
    _git(["add", "anything.txt"], codex_sandbox)

    result = _invoke_pre_commit(codex_sandbox)
    assert result.returncode == 0, (
        f"scope-guard should silent-pass when no task id parses: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Silent: no scope-guard error output
    assert "scope-guard rejected" not in result.stderr


# ── 2170: explicit bypass via TUSK_SCOPE_GUARD_BYPASS=1, override logged ─


def test_skip_verify_bypass(codex_sandbox):
    """TUSK_SCOPE_GUARD_BYPASS=1 short-circuits and logs the override.

    ``tusk commit --skip-verify`` passes ``--no-verify`` to git which already
    skips every pre-commit hook, but operators who want to bypass *only* this
    guard (or to make the bypass visible in stderr) can set the env var. The
    same flag is the testable seam — the override is logged so callers can
    audit when it fires.
    """
    task_id = _seed_task(
        codex_sandbox,
        "Touch in/scope.txt only",
        "This task only modifies the file at in/scope.txt and nothing else.",
    )
    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)

    (codex_sandbox / "out_of_scope.txt").write_text("nope\n")
    _git(["add", "out_of_scope.txt"], codex_sandbox)

    env = _sandbox_env(codex_sandbox)
    env["TUSK_SCOPE_GUARD_BYPASS"] = "1"
    result = _invoke_pre_commit(codex_sandbox, env=env)
    assert result.returncode == 0, (
        f"TUSK_SCOPE_GUARD_BYPASS=1 should bypass: stderr={result.stderr!r}"
    )
    # Override is logged to stderr
    assert "scope-guard: bypassed" in result.stderr
    assert "TUSK_SCOPE_GUARD_BYPASS" in result.stderr


# ── kill-switch (TUSK_NO_SCOPE_GUARD=1) ─────────────────────────────────


def test_kill_switch_silent_pass(codex_sandbox):
    """TUSK_NO_SCOPE_GUARD=1 short-circuits without logging."""
    task_id = _seed_task(
        codex_sandbox,
        "Touch in/scope.txt only",
        "This task only modifies the file at in/scope.txt and nothing else.",
    )
    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)

    (codex_sandbox / "out_of_scope.txt").write_text("nope\n")
    _git(["add", "out_of_scope.txt"], codex_sandbox)

    env = _sandbox_env(codex_sandbox)
    env["TUSK_NO_SCOPE_GUARD"] = "1"
    result = _invoke_pre_commit(codex_sandbox, env=env)
    assert result.returncode == 0
    # Kill-switch is silent: no bypass log line, no rejection text
    assert "scope-guard" not in result.stderr


# ── no scope signal (empty task_referenced_paths) -> silent pass ────────


def test_no_scope_signal_silent_pass(codex_sandbox):
    """A task with no referenced paths is unenforced (vacuous scope)."""
    task_id = _seed_task(
        codex_sandbox,
        "Vague task with no paths in description",
        "Make the thing work somehow.",
    )
    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)

    (codex_sandbox / "anything.txt").write_text("anything\n")
    _git(["add", "anything.txt"], codex_sandbox)

    result = _invoke_pre_commit(codex_sandbox)
    assert result.returncode == 0, (
        f"scope-guard should silent-pass when task has no scope signal: "
        f"stderr={result.stderr!r}"
    )
    assert "scope-guard rejected" not in result.stderr
