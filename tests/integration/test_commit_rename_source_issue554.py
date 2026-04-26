"""Integration tests for tusk-commit handling of `git mv` rename source paths (Issue #554).

After `git mv old new`, the index holds a single rename entry — the source
path is absent from disk and from `git ls-files`, but its deletion is staged.
Before the fix, passing both `old` and `new` to `tusk commit` errored with
`path not found: 'old'`. The fix extends `_get_staged_deletions` to recognise
rename source paths, so they pass the missing-files validation and are kept
out of the Step 3 `git add` call (where `git add <absent-path>` would itself
fail with `pathspec did not match any files`).
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
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["git", "-C", repo, "add", name], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", f"add {name}"], check=True)


def _run_commit(repo: str, message: str, *files: str, extra: tuple[str, ...] = ()):
    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    return subprocess.run(
        [
            "python3", TUSK_COMMIT_PY, repo, CONFIG_DEFAULT,
            "554", message, *files, "--skip-verify", *extra,
        ],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
    )


def _head_message(repo: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%B"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout


def _head_name_status(repo: str) -> str:
    """Return `git log -1 --name-status -M` for the HEAD commit.

    `-M` enables rename detection in the diff display. When the commit
    represents a rename, the entry shows as `R<score>\told\tnew`. When
    the rename detection fails (similarity below threshold), it shows as
    `D\told\nA\tnew` instead — exactly the regression the original Issue
    #554 reporter saw after dropping the source path from the args.
    """
    # `--format=` suppresses the commit-metadata header so the output is
    # just the name-status lines — otherwise `Author:` and other header
    # lines collide with simple status-line filters.
    return subprocess.run(
        ["git", "-C", repo, "log", "-1", "--name-status", "-M", "--format="],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout


class TestCommitAcceptsRenameSource:
    """`tusk commit old new` after `git mv old new` lands a rename commit."""

    def test_pure_rename_succeeds_and_preserves_rename_detection(self, tmp_path):
        """Rename with no working-tree edit on the destination: commit must
        land cleanly AND `git log -M` must report the change as a rename."""
        repo = str(tmp_path / "repo")
        _git_init(repo)
        # Seed a file with enough content that any similarity threshold
        # comfortably classifies the post-rename file as a rename match.
        _commit_file(repo, "old.txt", "alpha\nbeta\ngamma\ndelta\nepsilon\n")

        subprocess.run(["git", "-C", repo, "mv", "old.txt", "new.txt"], check=True)

        result = _run_commit(repo, "rename old to new", "old.txt", "new.txt")
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "path not found" not in (result.stdout + result.stderr)

        msg = _head_message(repo)
        assert msg.startswith("[TASK-554] rename old to new"), msg

        ns = _head_name_status(repo)
        # `git log -M` reports rename as `R<score>\told\tnew`.
        # Pin both halves: status starts with R and both paths appear.
        first_status_line = next(
            line for line in ns.splitlines() if line and line[0] in "RDAM"
        )
        assert first_status_line.startswith("R"), (
            f"expected rename status 'R...', got: {first_status_line!r}\n"
            f"full output:\n{ns}"
        )
        assert "old.txt" in ns and "new.txt" in ns

    def test_rename_with_destination_edit_succeeds(self, tmp_path):
        """RM scenario from Issue #554: `git mv` then edit the destination.

        `git status` reports `RM old -> new`; `git diff --cached --name-status`
        still reports it as a single `R<score>` entry because the working-tree
        modification is invisible to --cached. The commit must land without
        error.
        """
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _commit_file(
            repo,
            "ActivityView.swift",
            "import SwiftUI\n\nstruct ActivityView: View {\n    var body: some View {\n        Text(\"Activity\")\n    }\n}\n",
        )

        subprocess.run(
            ["git", "-C", repo, "mv", "ActivityView.swift", "LibraryView.swift"],
            check=True,
        )
        # Edit the destination — exact workflow from the issue body.
        with open(os.path.join(repo, "LibraryView.swift"), "w", encoding="utf-8") as f:
            f.write(
                "import SwiftUI\n\n"
                "struct LibraryView: View {\n"
                "    var body: some View {\n"
                "        Text(\"Library\")\n"
                "    }\n"
                "}\n"
            )

        result = _run_commit(
            repo,
            "Rename Activity tab to Library",
            "ActivityView.swift",
            "LibraryView.swift",
        )
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        # Original Issue #554 erroring on this exact path:
        assert "path not found: 'ActivityView.swift'" not in result.stderr

        # The destination must be in HEAD; the source must not.
        head_files = subprocess.run(
            ["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.splitlines()
        assert "LibraryView.swift" in head_files
        assert "ActivityView.swift" not in head_files

    def test_pure_d_deletion_still_works(self, tmp_path):
        """Regression guard: pure `git rm` deletion path is unaffected by the fix."""
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _commit_file(repo, "doomed.txt")

        subprocess.run(["git", "-C", repo, "rm", "doomed.txt"], check=True)

        result = _run_commit(repo, "remove doomed.txt", "doomed.txt")
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        head_files = subprocess.run(
            ["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.splitlines()
        assert "doomed.txt" not in head_files
