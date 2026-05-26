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


# ── TASK-471: scope-paths prefers task_scope over task_referenced_paths ────


def test_prefers_authoritative_scope(codex_sandbox):
    """When the authoritative ``task_scope`` table has rows, the guard
    reads those patterns and ignores the legacy
    ``task_referenced_paths`` hint cache extracted from the task
    description (criterion 2184).

    The task description references ``hint/legacy.txt`` (which would be in
    scope under the legacy fallback) but the operator declared
    ``declared/path.txt`` via ``tusk task-insert --scope``. The guard must
    reject a commit that stages ``hint/legacy.txt`` — proof that
    ``task_scope`` overrides the legacy cache — and accept a commit that
    stages ``declared/path.txt``.
    """
    env = _sandbox_env(codex_sandbox)
    result = _run(
        [
            "tusk", "task-insert",
            "Prefer authoritative scope",
            "Mention hint/legacy.txt in the description — this would land "
            "in task_referenced_paths under the legacy fallback.",
            "--criteria", "seed",
            "--scope", "declared/path.txt",
        ],
        codex_sandbox,
        env=env,
    )
    import json as _json
    task_id = int(_json.loads(result.stdout)["task_id"])

    # Verify scope-paths emits ONLY the declared pattern, not the legacy hint.
    sp = _run(["tusk", "scope-paths", str(task_id)], codex_sandbox, env=env)
    paths = [line for line in sp.stdout.splitlines() if line]
    assert paths == ["declared/path.txt"], (
        f"scope-paths must emit only task_scope patterns when present; got {paths}"
    )
    assert "hint/legacy.txt" not in paths

    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)

    # Staging the legacy-cache path must be rejected (task_scope wins).
    (codex_sandbox / "hint").mkdir(exist_ok=True)
    (codex_sandbox / "hint" / "legacy.txt").write_text("hint\n")
    _git(["add", "hint/legacy.txt"], codex_sandbox)
    rejected = _invoke_pre_commit(codex_sandbox, env=env)
    assert rejected.returncode != 0, (
        "guard must reject the legacy-cache path once task_scope is authoritative"
    )
    assert "scope-guard rejected" in rejected.stderr
    assert "hint/legacy.txt" in rejected.stderr

    # Reset the index, stage the declared path, and confirm the guard passes.
    _git(["reset"], codex_sandbox)
    (codex_sandbox / "declared").mkdir(exist_ok=True)
    (codex_sandbox / "declared" / "path.txt").write_text("declared\n")
    _git(["add", "declared/path.txt"], codex_sandbox)
    accepted = _invoke_pre_commit(codex_sandbox, env=env)
    assert accepted.returncode == 0, (
        f"guard must accept the operator-declared path: stderr={accepted.stderr!r}"
    )


def test_unbounded_silent_pass(codex_sandbox):
    """``task_scope`` rows with ``source='unbounded'`` make scope-paths
    emit nothing, so the guard silent-passes any staged file. The same
    flag prevents the description's referenced paths from ever being
    enforced — opting out of the entire restriction is the explicit
    contract."""
    env = _sandbox_env(codex_sandbox)
    result = _run(
        [
            "tusk", "task-insert",
            "Unbounded refactor",
            "Touches anywhere/across/the/repo.txt — unbounded opts out of "
            "the scope guard entirely.",
            "--criteria", "seed",
            "--unbounded",
        ],
        codex_sandbox,
        env=env,
    )
    import json as _json
    task_id = int(_json.loads(result.stdout)["task_id"])

    sp = _run(["tusk", "scope-paths", str(task_id)], codex_sandbox, env=env)
    assert sp.stdout.strip() == "", (
        f"unbounded must suppress all patterns; got {sp.stdout!r}"
    )

    _git(["checkout", "-b", f"feature/TASK-{task_id}-x"], codex_sandbox)
    (codex_sandbox / "anything_at_all.txt").write_text("anywhere\n")
    _git(["add", "anything_at_all.txt"], codex_sandbox)
    accepted = _invoke_pre_commit(codex_sandbox, env=env)
    assert accepted.returncode == 0, (
        f"unbounded scope must silent-pass any staged file: stderr={accepted.stderr!r}"
    )


# ── Issue #886: config.default.json / config.json bare-name extraction ─────


def test_scope_paths_extracts_config_default_json_bare_name(codex_sandbox):
    """A description that names ``config.default.json`` without a directory
    prefix must extract through ``_BARE_TOPLEVEL_WHITELIST`` so the
    commit-time guard authorises edits to that file (issue #886)."""
    env = _sandbox_env(codex_sandbox)
    result = _run(
        [
            "tusk", "task-insert",
            "Add foo key to config",
            "Edit config.default.json to introduce the foo key.",
            "--criteria", "seed",
        ],
        codex_sandbox,
        env=env,
    )
    import json as _json
    task_id = int(_json.loads(result.stdout)["task_id"])

    sp = _run(["tusk", "scope-paths", str(task_id)], codex_sandbox, env=env)
    paths = [line for line in sp.stdout.splitlines() if line]
    assert "config.default.json" in paths, (
        f"config.default.json must be extracted from the description; got {paths}"
    )


def test_scope_paths_extracts_config_json_bare_name(codex_sandbox):
    """Same as above for the live ``config.json`` filename (issue #886)."""
    env = _sandbox_env(codex_sandbox)
    result = _run(
        [
            "tusk", "task-insert",
            "Tune live config",
            "Rewrite config.json review block to use ai_only mode.",
            "--criteria", "seed",
        ],
        codex_sandbox,
        env=env,
    )
    import json as _json
    task_id = int(_json.loads(result.stdout)["task_id"])

    sp = _run(["tusk", "scope-paths", str(task_id)], codex_sandbox, env=env)
    paths = [line for line in sp.stdout.splitlines() if line]
    assert "config.json" in paths, (
        f"config.json must be extracted from the description; got {paths}"
    )


# ── Issue #891: Rule-42 companions auto-union for new bin/tusk-*.py scripts ──


def test_scope_paths_auto_unions_rule42_companions_for_new_tusk_script(codex_sandbox):
    """When a task description references a ``bin/tusk-*.py`` path that is
    not yet tracked by git, ``tusk scope-paths`` augments the output with
    ``bin/tusk`` + ``MANIFEST`` + ``.claude/tusk-manifest.json`` so Rule 42's
    same-commit choreography lands without TUSK_SCOPE_GUARD_BYPASS=1
    (issue #891)."""
    env = _sandbox_env(codex_sandbox)
    result = _run(
        [
            "tusk", "task-insert",
            "Add bin/tusk-fresh-script.py",
            "Create bin/tusk-fresh-script.py to do the thing.",
            "--criteria", "seed",
        ],
        codex_sandbox,
        env=env,
    )
    import json as _json
    task_id = int(_json.loads(result.stdout)["task_id"])

    sp = _run(["tusk", "scope-paths", str(task_id)], codex_sandbox, env=env)
    paths = [line for line in sp.stdout.splitlines() if line]
    assert "bin/tusk-fresh-script.py" in paths
    assert "bin/tusk" in paths, (
        f"Rule-42 dispatcher companion missing for a new bin/tusk-*.py task; got {paths}"
    )
    assert "MANIFEST" in paths, (
        f"Rule-42 MANIFEST companion missing for a new bin/tusk-*.py task; got {paths}"
    )
    assert ".claude/tusk-manifest.json" in paths, (
        f"Rule-42 .claude/tusk-manifest.json companion missing; got {paths}"
    )


def test_scope_paths_no_companions_for_existing_tusk_script(codex_sandbox):
    """If every referenced ``bin/tusk-*.py`` path is already tracked by git,
    the augmentation must not fire — existing-script edits do not need the
    Rule-42 same-commit choreography."""
    env = _sandbox_env(codex_sandbox)
    # The codex sandbox puts tusk binaries under tusk/bin/, not bin/, so
    # no bin/tusk-*.py exists by default. Pre-create + commit one so the
    # untracked-script branch does NOT fire when the task references it.
    (codex_sandbox / "bin").mkdir(exist_ok=True)
    tracked_script = "bin/tusk-already-tracked.py"
    (codex_sandbox / tracked_script).write_text("# pre-existing\n", encoding="utf-8")
    _git(["add", tracked_script], codex_sandbox)
    _git(["commit", "-m", "seed existing script"], codex_sandbox)

    result = _run(
        [
            "tusk", "task-insert",
            "Tweak existing script",
            f"Patch a regression in {tracked_script} only.",
            "--criteria", "seed",
        ],
        codex_sandbox,
        env=env,
    )
    import json as _json
    task_id = int(_json.loads(result.stdout)["task_id"])

    sp = _run(["tusk", "scope-paths", str(task_id)], codex_sandbox, env=env)
    paths = [line for line in sp.stdout.splitlines() if line]
    assert tracked_script in paths
    assert "bin/tusk" not in paths, (
        f"existing-script tasks must not auto-include Rule-42 companions; got {paths}"
    )
    assert "MANIFEST" not in paths
    assert ".claude/tusk-manifest.json" not in paths
