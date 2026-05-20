"""Integration tests for tusk version-bump worktree resolution (issues #798, #801).

Regression: before the fix, `cmd_version_bump` resolved VERSION via
`$SCRIPT_DIR/VERSION` then `$INSTALL_DIR/VERSION` — both pointing at the source
bin's install location. From a task-owned worktree (where `$REPO_ROOT` is the
worktree path, not the install root), the subsequent
`git -C "$REPO_ROOT" add "$version_file"` refused with
"<source>/VERSION is outside repository at <worktree>" and exit 128. The fix
inserts `$REPO_ROOT/VERSION` as the first candidate, mirroring
tusk-changelog-add.py's worktree-aware behavior.

Cases covered:
- Bumping VERSION from a linked git worktree writes the worktree's VERSION
  file, exits 0, and stages the change in the worktree's index.
- Bumping VERSION from a primary checkout (where `$REPO_ROOT == $SCRIPT_DIR`)
  still works — the new $REPO_ROOT-first lookup picks up the same file.
"""

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


def _tusk(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _seed_repo_with_version(tmp_path, initial="909"):
    """Build a minimal git repo with a VERSION file and an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "VERSION").write_text(f"{initial}\n", encoding="utf-8")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "VERSION", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _env_with_db(repo):
    env = os.environ.copy()
    env["TUSK_DB"] = str(repo / "tusk" / "tasks.db")
    env["TUSK_QUIET"] = "1"
    return env


def test_version_bump_from_primary_checkout_still_works(tmp_path):
    """Primary checkout (REPO_ROOT == SCRIPT_DIR) — no behavior change."""
    repo = _seed_repo_with_version(tmp_path, initial="100")
    env = _env_with_db(repo)

    result = _tusk(["version-bump"], cwd=repo, env=env)

    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert (repo / "VERSION").read_text().strip() == "101"
    # File should be staged in the index.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "VERSION" in staged.stdout.splitlines()


def test_version_bump_from_worktree_writes_worktree_version(tmp_path):
    """The issue #801/#798 regression: bumping from a linked worktree must
    write the worktree's VERSION file and stage it cleanly (exit 0), not
    fall through to the source-bin path and trip 'outside repository at'.
    """
    repo = _seed_repo_with_version(tmp_path, initial="500")
    # Create a linked worktree on a feature branch.
    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/task-bump"], cwd=repo)

    # The worktree's VERSION starts at the same value (it's tracked).
    assert (worktree / "VERSION").read_text().strip() == "500"

    env = _env_with_db(repo)
    result = _tusk(["version-bump"], cwd=worktree, env=env)

    assert result.returncode == 0, (
        f"expected exit 0 from worktree bump; got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    # The bumped value must land in the WORKTREE's VERSION, not the primary's.
    assert (worktree / "VERSION").read_text().strip() == "501"
    # The primary checkout's VERSION must NOT have moved.
    assert (repo / "VERSION").read_text().strip() == "500"
    # And the worktree's VERSION must be staged in the worktree's index.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "VERSION" in staged.stdout.splitlines()


def test_version_bump_from_worktree_does_not_touch_primary_index(tmp_path):
    """Belt-and-suspenders: the worktree bump must NOT stage anything in the
    primary repo's index. Before the fix, the failed git add never landed
    anywhere; after the fix, the staged path is the worktree's VERSION only.
    """
    repo = _seed_repo_with_version(tmp_path, initial="700")
    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/task-bump2"], cwd=repo)

    env = _env_with_db(repo)
    result = _tusk(["version-bump"], cwd=worktree, env=env)
    assert result.returncode == 0, result.stderr

    # Primary checkout's index should be empty (nothing staged here).
    primary_staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert primary_staged.stdout.strip() == "", (
        f"primary checkout's index should be empty; got: {primary_staged.stdout!r}"
    )
