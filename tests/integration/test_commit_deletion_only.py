"""Integration tests for tusk-commit handling of already-staged deletions (TASK-67).

Covers the scenario from TASK-60 where `git rm` was used to stage deletions,
then `tusk commit` was invoked with those paths. Previously the pathspec-mismatch
retry ran `git add -f` which silently re-added the deleted files and defeated
the deletion. The fix partitions staged deletions out of the Step 3 `git add`
call so the deletion survives and the commit still gets the [TASK-N] prefix,
Co-Authored-By trailer, and lint/test gates.
"""

import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")


def _git_init(repo: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)


def _commit_file(repo: str, name: str, content: str = "seed\n") -> None:
    path = os.path.join(repo, name)
    with open(path, "w") as f:
        f.write(content)
    subprocess.run(["git", "-C", repo, "add", name], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", f"add {name}"], check=True)


def _run_commit(repo: str, message: str, *files: str, extra: tuple[str, ...] = ()):
    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    # --skip-verify bypasses lint (fixture isn't a full tusk install) but the
    # [TASK-N] prefix + Co-Authored-By trailer paths are independent of lint.
    return subprocess.run(
        [
            "python3", TUSK_COMMIT_PY, repo, CONFIG_DEFAULT,
            "999", message, *files, "--skip-verify", *extra,
        ],
        capture_output=True, text=True, cwd=repo, env=env,
    )


def _head_message(repo: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%B"],
        capture_output=True, text=True, check=True,
    ).stdout


def _head_tree_has(repo: str, name: str) -> bool:
    r = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return name in r.stdout.splitlines()


class TestDeletionOnlyCommit:
    def test_git_rm_path_commits_cleanly(self, tmp_path):
        """`git rm` + `tusk commit <path>` removes the file and lands a commit."""
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _commit_file(repo, "a.txt")

        subprocess.run(["git", "-C", repo, "rm", "a.txt"], check=True)

        result = _run_commit(repo, "remove a.txt", "a.txt")
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        msg = _head_message(repo)
        assert msg.startswith("[TASK-999] remove a.txt"), msg
        assert "Co-Authored-By:" in msg, msg
        assert not _head_tree_has(repo, "a.txt"), "a.txt must not be in HEAD"

    def test_deletion_of_gitignored_tracked_file(self, tmp_path):
        """Regression guard for TASK-60: gitignored-but-tracked file, then `git rm`.

        Before the fix, the pathspec-mismatch retry matched the .gitignore
        rule and ran `git add -f`, silently re-adding the deleted file.
        """
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _commit_file(repo, "tracked.txt")
        # Add tracked.txt to .gitignore AFTER it was committed so the file
        # is both tracked and ignored-going-forward — mirrors TASK-60's
        # scenario where previously-tracked build artifacts were being
        # excised from the repo.
        with open(os.path.join(repo, ".gitignore"), "w") as f:
            f.write("tracked.txt\n")
        subprocess.run(["git", "-C", repo, "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", repo, "commit", "-q", "-m", "ignore tracked.txt"], check=True
        )

        subprocess.run(["git", "-C", repo, "rm", "tracked.txt"], check=True)

        result = _run_commit(repo, "remove tracked.txt", "tracked.txt")
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert not _head_tree_has(repo, "tracked.txt"), (
            "tracked.txt must not be in HEAD — the fix prevents git add -f "
            "from re-adding it via the gitignore retry branch"
        )

    def test_mixed_deletion_and_addition(self, tmp_path):
        """`git rm` one file, create another, pass both to tusk commit."""
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _commit_file(repo, "a.txt")

        subprocess.run(["git", "-C", repo, "rm", "a.txt"], check=True)
        with open(os.path.join(repo, "c.txt"), "w") as f:
            f.write("new\n")

        result = _run_commit(repo, "del a, add c", "a.txt", "c.txt")
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert not _head_tree_has(repo, "a.txt")
        assert _head_tree_has(repo, "c.txt")
