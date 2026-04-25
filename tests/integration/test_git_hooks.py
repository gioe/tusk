"""Integration tests for git-event hook dispatchers (TASK-155).

Verifies that a Codex-installed sandbox enforces all six guard hooks at
git-event time via the dispatchers written into .git/hooks/ by install.sh.

Each guard is exercised end-to-end: the dispatcher is invoked as a script
(same way git would invoke it), and the test asserts a non-zero exit on the
failure case plus a zero exit on the success case.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")

MARKER = "TUSK_HOOK_DISPATCHER_V1"


def _run(cmd, cwd, check=True, input_bytes=None):
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_bytes,
    )
    if check:
        assert result.returncode == 0, (
            f"command {cmd} failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _git(args, cwd, check=True):
    return _run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", *args], cwd, check=check)


@pytest.fixture()
def codex_sandbox(tmp_path):
    """A codex-layout git repo with tusk already installed via install.sh."""
    _run(["git", "init"], tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    _run(["bash", INSTALL_SH], tmp_path)
    return tmp_path


def _invoke_hook(sandbox, event, *args, input_bytes=None):
    """Invoke .git/hooks/<event> directly and return the CompletedProcess."""
    return subprocess.run(
        [str(sandbox / ".git" / "hooks" / event), *args],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        input=input_bytes,
    )


# ── Structure / install artifacts ────────────────────────────────────────


def test_dispatchers_installed_with_marker(codex_sandbox):
    """install.sh writes all three dispatchers with the TUSK_HOOK_DISPATCHER marker."""
    for event in ("pre-commit", "pre-push", "commit-msg"):
        path = codex_sandbox / ".git" / "hooks" / event
        assert path.exists(), f".git/hooks/{event} should be installed"
        assert os.access(str(path), os.X_OK), f".git/hooks/{event} should be executable"
        assert MARKER in path.read_text(), (
            f".git/hooks/{event} should carry the {MARKER} marker"
        )


def test_guards_installed_into_install_dir(codex_sandbox):
    """All six guard scripts land under tusk/bin/hooks/git/ in codex mode."""
    guards = [
        "block-raw-sqlite",
        "block-sql-neq",
        "branch-naming",
        "commit-msg-format",
        "version-bump-check",
        "dupe-gate",
    ]
    for guard in guards:
        path = codex_sandbox / "tusk" / "bin" / "hooks" / "git" / f"{guard}.sh"
        assert path.exists(), f"{guard}.sh should be installed"
        assert os.access(str(path), os.X_OK), f"{guard}.sh should be executable"


def test_install_summary_lists_git_hooks_entries(tmp_path):
    """install.sh summary output lists the .git/hooks/ entries it installed."""
    _run(["git", "init"], tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    result = subprocess.run(
        ["bash", INSTALL_SH],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for event in ("pre-commit", "pre-push", "commit-msg"):
        assert f".git/hooks/{event}" in result.stdout, (
            f"install.sh summary should mention .git/hooks/{event}"
        )


# ── Chaining / idempotency ───────────────────────────────────────────────


def test_existing_hook_is_chained_not_overwritten(tmp_path):
    """A pre-existing non-tusk hook is renamed to <event>.pre-tusk and invoked."""
    _run(["git", "init"], tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    hooks_dir = tmp_path / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text(
        "#!/bin/bash\necho USER-HOOK-FIRED\nexit 0\n"
    )
    pre_commit.chmod(0o755)

    _run(["bash", INSTALL_SH], tmp_path)

    chained = hooks_dir / "pre-commit.pre-tusk"
    assert chained.exists(), "existing hook should be preserved as pre-commit.pre-tusk"
    assert "USER-HOOK-FIRED" in chained.read_text(), (
        "chained hook content should be the original user hook"
    )
    assert MARKER in pre_commit.read_text(), "new pre-commit should be a tusk dispatcher"

    # Dispatcher should invoke the chained hook
    result = subprocess.run(
        [str(pre_commit)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "USER-HOOK-FIRED" in result.stdout


def test_reinstall_is_idempotent(codex_sandbox):
    """Re-running install.sh preserves an already-chained .pre-tusk hook."""
    chained = codex_sandbox / ".git" / "hooks" / "pre-commit.pre-tusk"
    # Plant a fake chained hook and a tusk dispatcher (simulating first install)
    chained.write_text("#!/bin/bash\necho ALREADY-CHAINED\nexit 0\n")
    chained.chmod(0o755)
    # The first install already wrote the dispatcher; capture its content
    dispatcher = codex_sandbox / ".git" / "hooks" / "pre-commit"
    before = dispatcher.read_text()

    _run(["bash", INSTALL_SH], codex_sandbox)

    assert chained.exists(), ".pre-tusk hook should not be overwritten on re-run"
    assert "ALREADY-CHAINED" in chained.read_text()
    # Dispatcher is rewritten (deterministic content) but the marker persists
    after = dispatcher.read_text()
    assert MARKER in after
    # Content is deterministic: re-run produces same dispatcher
    assert before == after


# ── Per-guard enforcement ────────────────────────────────────────────────


def test_branch_naming_blocks_bad_branch(codex_sandbox):
    """pre-push rejects a non-feature/TASK-<id>-* branch (criterion 675)."""
    # Need one commit to create the branch off of
    _git(["checkout", "-b", "bad-branch"], codex_sandbox)
    (codex_sandbox / "x.txt").write_text("x\n")
    _git(["add", "x.txt"], codex_sandbox)
    _git(["commit", "--no-verify", "-m", "seed"], codex_sandbox)

    result = _invoke_hook(codex_sandbox, "pre-push", "origin", "http://example.git",
                          input_bytes="")
    assert result.returncode != 0, "pre-push should reject non-feature branch names"
    assert "bad-branch" in result.stderr
    assert "feature/TASK-" in result.stderr


def test_branch_naming_allows_feature_branch(codex_sandbox):
    """pre-push allows a properly-named feature/TASK-<id>-* branch."""
    _git(["checkout", "-b", "feature/TASK-42-smoke"], codex_sandbox)
    (codex_sandbox / "x.txt").write_text("x\n")
    _git(["add", "x.txt"], codex_sandbox)
    _git(["commit", "--no-verify", "-m", "[TASK-42] seed"], codex_sandbox)

    result = _invoke_hook(codex_sandbox, "pre-push", "origin", "http://example.git",
                          input_bytes="")
    assert result.returncode == 0, (
        f"pre-push should allow feature branches: stderr={result.stderr!r}"
    )


def test_commit_msg_format_blocks_bad_message(codex_sandbox):
    """commit-msg rejects a message missing [TASK-<id>] on a feature branch (criterion 676)."""
    _git(["checkout", "-b", "feature/TASK-7-x"], codex_sandbox)
    msg_file = codex_sandbox / ".git" / "COMMIT_EDITMSG_TEST"
    msg_file.write_text("change stuff\n")

    result = _invoke_hook(codex_sandbox, "commit-msg", str(msg_file))
    assert result.returncode != 0, "commit-msg should reject messages without [TASK-N] prefix"
    # Must carry the existing commit-msg-format warning text
    assert "does not start with [TASK-" in result.stderr


def test_commit_msg_format_allows_good_message(codex_sandbox):
    """commit-msg allows a message with the [TASK-<id>] prefix."""
    _git(["checkout", "-b", "feature/TASK-7-x"], codex_sandbox)
    msg_file = codex_sandbox / ".git" / "COMMIT_EDITMSG_TEST"
    msg_file.write_text("[TASK-7] add widget\n")

    result = _invoke_hook(codex_sandbox, "commit-msg", str(msg_file))
    assert result.returncode == 0, (
        f"commit-msg should accept proper [TASK-N] prefix: stderr={result.stderr!r}"
    )


def test_block_raw_sqlite_blocks_staged_invocation(codex_sandbox):
    """pre-commit rejects a staged .sh file containing a raw sqlite3 invocation."""
    _git(["checkout", "-b", "feature/TASK-1-x"], codex_sandbox)
    (codex_sandbox / "bad.sh").write_text('sqlite3 /tmp/x "SELECT 1"\n')
    _git(["add", "bad.sh"], codex_sandbox)

    result = _invoke_hook(codex_sandbox, "pre-commit")
    assert result.returncode != 0, "pre-commit should reject raw sqlite3 invocations"
    assert "sqlite3" in result.stderr


def test_block_sql_neq_blocks_staged_sql(codex_sandbox):
    """pre-commit rejects a staged .sh file containing SQL `!=`."""
    _git(["checkout", "-b", "feature/TASK-1-x"], codex_sandbox)
    (codex_sandbox / "bad.sh").write_text(
        "tusk shell \"SELECT * FROM tasks WHERE status != 'Done'\"\n"
    )
    _git(["add", "bad.sh"], codex_sandbox)

    result = _invoke_hook(codex_sandbox, "pre-commit")
    assert result.returncode != 0, "pre-commit should reject SQL '!=' operator"
    assert "!=" in result.stderr


def test_dupe_gate_blocks_staged_duplicate_insert(codex_sandbox):
    """pre-commit rejects a staged INSERT INTO tasks(...) with a duplicate summary."""
    tusk = str(codex_sandbox / "tusk" / "bin" / "tusk")
    # Seed a task so the dupe check has something to match against
    _run([tusk, "task-insert", "Refactor the widget loader", "Placeholder",
          "--criteria", "seed criterion"],
         codex_sandbox)

    _git(["checkout", "-b", "feature/TASK-1-x"], codex_sandbox)
    (codex_sandbox / "bad.sh").write_text(
        "tusk shell \"INSERT INTO tasks(summary, description) "
        "VALUES ('Refactor the widget loader', 'again')\"\n"
    )
    _git(["add", "bad.sh"], codex_sandbox)

    env = os.environ.copy()
    env["PATH"] = str(codex_sandbox / "tusk" / "bin") + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [str(codex_sandbox / ".git" / "hooks" / "pre-commit")],
        cwd=str(codex_sandbox),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, "pre-commit should reject duplicate task INSERTs"
    assert "duplicate" in result.stderr.lower()


def test_version_bump_check_blocks_missing_bump(codex_sandbox, tmp_path_factory):
    """version-bump-check guard rejects distributable change without VERSION bump.

    The guard is only wired into the pre-push dispatcher in source-mode installs
    (issue #558) — codex_sandbox is a consumer install — so this test invokes
    the guard script directly to verify its logic is intact. The guard *file*
    is still copied unconditionally in both roles.
    """
    bare = tmp_path_factory.mktemp("bare") / "origin.git"
    _run(["git", "init", "--bare", str(bare)], codex_sandbox.parent)
    _git(["remote", "add", "origin", str(bare)], codex_sandbox)

    # Seed main with an install.sh so a later change shows up in diff
    (codex_sandbox / "install.sh").write_text("# placeholder\n")
    (codex_sandbox / "VERSION").write_text("1\n")
    _git(["add", "install.sh", "VERSION"], codex_sandbox)
    _git(["commit", "--no-verify", "-m", "[TASK-0] seed"], codex_sandbox)
    _git(["push", "--no-verify", "-u", "origin", "main"], codex_sandbox)

    # Feature branch that modifies install.sh (a distributable) without bumping VERSION
    _git(["checkout", "-b", "feature/TASK-9-change"], codex_sandbox)
    (codex_sandbox / "install.sh").write_text("# placeholder\n# change\n")
    _git(["add", "install.sh"], codex_sandbox)
    _git(["commit", "--no-verify", "-m", "[TASK-9] modify install.sh"], codex_sandbox)

    guard = codex_sandbox / "tusk" / "bin" / "hooks" / "git" / "version-bump-check.sh"
    assert guard.exists(), "version-bump-check guard file must still be installed"
    result = subprocess.run(
        [str(guard), "origin", str(bare)],
        cwd=str(codex_sandbox),
        capture_output=True,
        text=True,
        input="",
    )
    assert result.returncode != 0, (
        "version-bump-check guard should reject distributable change without VERSION bump"
    )
    assert "VERSION" in result.stderr


def test_consumer_pre_push_dispatcher_does_not_invoke_version_bump_check(
    codex_sandbox, tmp_path_factory,
):
    """Issue #558 regression: pre-push in a consumer install must not run version-bump-check.

    Same setup as the guard-direct test above, but invokes the dispatcher. The
    push should pass through because version-bump-check is omitted from the
    consumer-mode dispatcher — it would otherwise silently no-op on every push
    in any consumer that lacks the source-repo path layout.
    """
    bare = tmp_path_factory.mktemp("bare") / "origin.git"
    _run(["git", "init", "--bare", str(bare)], codex_sandbox.parent)
    _git(["remote", "add", "origin", str(bare)], codex_sandbox)
    (codex_sandbox / "install.sh").write_text("# placeholder\n")
    (codex_sandbox / "VERSION").write_text("1\n")
    _git(["add", "install.sh", "VERSION"], codex_sandbox)
    _git(["commit", "--no-verify", "-m", "[TASK-0] seed"], codex_sandbox)
    _git(["push", "--no-verify", "-u", "origin", "main"], codex_sandbox)

    _git(["checkout", "-b", "feature/TASK-9-change"], codex_sandbox)
    (codex_sandbox / "install.sh").write_text("# placeholder\n# change\n")
    _git(["add", "install.sh"], codex_sandbox)
    _git(["commit", "--no-verify", "-m", "[TASK-9] modify install.sh"], codex_sandbox)

    result = _invoke_hook(codex_sandbox, "pre-push", "origin", str(bare),
                          input_bytes="")
    assert result.returncode == 0, (
        "consumer-mode pre-push should not block on missing VERSION bump; "
        f"stderr was: {result.stderr!r}"
    )
