"""Regression tests for GitHub Issue #360 (TASK-630).

When lint-staged (or another pre-commit hook) reformats and re-stages files
before tusk commit is invoked, git add may fail with "pathspec did not match
any files" because the files are already fully staged and the working tree
matches the index.  tusk commit must detect this case and proceed to commit
rather than aborting with exit code 3.

This suite specifically exercises the combination of:
  - caller invoked from a monorepo subdirectory (not the repo root)
  - repo-root-relative paths passed as arguments
  - lint-staged having pre-staged the files

It also covers both file states required by the acceptance criteria:
  - modified-and-staged (working tree == index, nothing for git add to do but it succeeds)
  - modified-but-unstaged (git add stages normally and succeeds)
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_completed(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestLintStagedSubdirIssue360:
    """Regression for GitHub Issue #360: tusk commit fails with pathspec error
    when invoked from a monorepo subdirectory with repo-root-relative paths
    after lint-staged has pre-staged the files."""

    def test_lint_staged_restaged_from_subdir_repo_root_relative_paths(self, tmp_path, capsys):
        """Primary regression: called from subdir, repo-root-relative paths,
        git add fails (lint-staged already staged files), all files in cache → exit 0.

        Exact Issue #360 scenario:
          - CWD = apps/web (a monorepo subdirectory)
          - paths passed: apps/web/ui/components/cards/show/index.tsx (repo-root-relative)
          - lint-staged already ran: files are fully staged in the index
          - git add returns exit 128 "pathspec did not match any files"
          - tusk commit must detect all files are cached and proceed to commit
        """
        mod = _load_module()

        # Create the file in the monorepo layout
        subdir = tmp_path / "apps" / "web"
        subdir.mkdir(parents=True)
        target = tmp_path / "apps" / "web" / "ui" / "components" / "cards" / "show" / "index.tsx"
        target.parent.mkdir(parents=True)
        target.write_text("// card component")

        config = tmp_path / "config.json"
        config.write_text("{}")

        # Invoked from apps/web with repo-root-relative paths
        argv = [
            str(tmp_path), str(config), "279", "fix card layout",
            "apps/web/ui/components/cards/show/index.tsx",
        ]

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr=(
                        "fatal: pathspec 'apps/web/ui/components/cards/show/"
                        "index.tsx' did not match any files"
                    ),
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(
                    0, stdout="apps/web/ui/components/cards/show/index.tsx\n"
                )
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] fix card layout")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Error:" not in captured.err
        assert "already staged" in captured.out

    def test_multiple_files_lint_staged_from_subdir(self, tmp_path):
        """Multiple repo-root-relative files from subdirectory, all pre-staged by lint-staged → exit 0."""
        mod = _load_module()

        subdir = tmp_path / "apps" / "web"
        subdir.mkdir(parents=True)
        for name in ["show/index.tsx", "popular/index.tsx"]:
            f = tmp_path / "apps" / "web" / "ui" / "components" / "cards" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("// component")

        config = tmp_path / "config.json"
        config.write_text("{}")

        argv = [
            str(tmp_path), str(config), "279", "fix cards",
            "apps/web/ui/components/cards/show/index.tsx",
            "apps/web/ui/components/cards/popular/index.tsx",
        ]

        # git add fails for both; both are in the cache.
        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr=(
                        "fatal: pathspec 'apps/web/ui/components/cards/show/"
                        "index.tsx' did not match any files"
                    ),
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(
                    0,
                    stdout=(
                        "apps/web/ui/components/cards/show/index.tsx\n"
                        "apps/web/ui/components/cards/popular/index.tsx\n"
                    ),
                )
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] fix cards")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 0

    def test_partial_lint_staged_from_subdir_exits_3(self, tmp_path, capsys):
        """If only SOME files are pre-staged, tusk commit must still exit 3.

        Lint-staged may only process files matching its patterns.  If it staged
        file A but not file B, git add fails and the cache check for file B will
        miss — the error must propagate so the user knows not all files are ready.
        """
        mod = _load_module()

        subdir = tmp_path / "apps" / "web"
        subdir.mkdir(parents=True)
        for name in ["show/index.tsx", "popular/index.tsx"]:
            f = tmp_path / "apps" / "web" / "ui" / "components" / "cards" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("// component")

        config = tmp_path / "config.json"
        config.write_text("{}")

        argv = [
            str(tmp_path), str(config), "279", "fix cards",
            "apps/web/ui/components/cards/show/index.tsx",
            "apps/web/ui/components/cards/popular/index.tsx",
        ]

        # git add fails; only the first file is in the cache (partial lint-staged run).
        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr="fatal: pathspec 'apps/web/...' did not match any files",
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(
                    0,
                    stdout="apps/web/ui/components/cards/show/index.tsx\n",  # only one
                )
            if args[:3] == ["git", "check-ignore", "-v"]:
                return _make_completed(1)  # not ignored
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "Error: git add failed" in captured.err


class TestFileStatesFromSubdir:
    """Verify tusk commit handles both modified-and-staged and modified-but-unstaged
    file states correctly when invoked from a monorepo subdirectory (criterion 5)."""

    def _make_argv(self, tmp_path, subdir, files):
        config = tmp_path / "config.json"
        config.write_text("{}")
        return [str(tmp_path), str(config), "279", "fix"] + files

    def test_modified_but_unstaged_from_subdir(self, tmp_path):
        """Modified-but-unstaged file: git add succeeds normally → exit 0.

        The file exists in the working tree with modifications but is not yet
        staged.  git add stages it (returns 0) and the commit proceeds.
        """
        mod = _load_module()

        subdir = tmp_path / "apps" / "web"
        subdir.mkdir(parents=True)
        target = tmp_path / "apps" / "web" / "ui" / "index.tsx"
        target.parent.mkdir()
        target.write_text("// modified but not staged")

        argv = self._make_argv(tmp_path, subdir, ["apps/web/ui/index.tsx"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)  # normal success — file staged
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] fix")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 0
        # git add must have received the correct repo-root-relative path
        assert "--" in captured_add_args
        idx = captured_add_args.index("--")
        git_add_path = captured_add_args[idx + 1]
        assert "apps/web/ui/index.tsx" in git_add_path
        assert ".." not in git_add_path

    def test_modified_and_staged_from_subdir(self, tmp_path):
        """Modified-and-staged file: git add is a no-op (returns 0) → exit 0.

        lint-staged (or the user) has already staged the file.  The working tree
        matches the index.  git add returns 0 silently (nothing to update).
        The commit proceeds normally.
        """
        mod = _load_module()

        subdir = tmp_path / "apps" / "web"
        subdir.mkdir(parents=True)
        target = tmp_path / "apps" / "web" / "ui" / "index.tsx"
        target.parent.mkdir()
        target.write_text("// already staged")

        argv = self._make_argv(tmp_path, subdir, ["apps/web/ui/index.tsx"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                # Simulate: file already staged, git add is silent no-op (exit 0)
                return _make_completed(0, stdout="", stderr="")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] fix")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 0
        # git add still called with the correct path
        assert "--" in captured_add_args
        idx = captured_add_args.index("--")
        git_add_path = captured_add_args[idx + 1]
        assert "apps/web/ui/index.tsx" in git_add_path
        assert ".." not in git_add_path

    def test_modified_and_staged_pathspec_fallback_from_subdir(self, tmp_path, capsys):
        """Modified-and-staged, but git add returns pathspec error (lint-staged edge case).

        In rare git/lint-staged interactions, git add may fail with "pathspec did not
        match" even when the file is fully staged.  tusk commit must fall back to
        checking the cache and proceed if the file is there.
        """
        mod = _load_module()

        subdir = tmp_path / "apps" / "web"
        subdir.mkdir(parents=True)
        target = tmp_path / "apps" / "web" / "ui" / "index.tsx"
        target.parent.mkdir()
        target.write_text("// staged by lint-staged")

        argv = self._make_argv(tmp_path, subdir, ["apps/web/ui/index.tsx"])

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr=(
                        "fatal: pathspec 'apps/web/ui/index.tsx' "
                        "did not match any files"
                    ),
                )
            if args[:3] == ["git", "ls-files", "--cached"]:
                return _make_completed(0, stdout="apps/web/ui/index.tsx\n")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] fix")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 0
        captured = capsys.readouterr()
        assert "already staged" in captured.out
        assert "Error:" not in captured.err
