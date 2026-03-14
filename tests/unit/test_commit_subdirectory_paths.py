"""Unit tests for tusk-commit.py subdirectory path resolution.

Verifies that file paths relative to the caller's CWD (e.g. inside a
monorepo subdirectory) are resolved to repo-root-relative paths before
being passed to `git add`, fixing the pathspec error described in
GitHub Issue #336.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

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


def _argv(tmp_path, files=None):
    config = tmp_path / "config.json"
    config.write_text("{}")
    if files is None:
        (tmp_path / "somefile.py").write_text("")
    return [str(tmp_path), str(config), "42", "my message"] + (files or ["somefile.py"])


class TestDoubledPrefixRegression:
    """Regression: path prefix is not doubled when caller_cwd is a subdirectory
    whose name matches the first component of the passed path (GitHub Issue #344)."""

    def test_repo_root_relative_path_from_matching_subdir(self, tmp_path):
        """tusk commit from inside svc/ with path svc/app/foo.py must not double-prefix."""
        mod = _load_module()

        # Repo layout: tmp_path/svc/app/foo.py
        svc_dir = tmp_path / "svc"
        app_dir = svc_dir / "app"
        app_dir.mkdir(parents=True)
        target = app_dir / "foo.py"
        target.write_text("# foo")

        # User is inside tmp_path/svc/ and passes the repo-root-relative path
        argv = _argv(tmp_path, files=["svc/app/foo.py"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] commit")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(svc_dir)):
            rc = mod.main(argv)

        assert rc == 0
        assert captured_add_args[0] == "--"
        assert captured_add_args[1] == os.path.join("svc", "app", "foo.py")

    def test_cwd_relative_preferred_when_both_exist(self, tmp_path):
        """When both CWD-relative and repo-root-relative paths exist, CWD-relative wins."""
        mod = _load_module()

        # Repo layout: two files that could match:
        #   tmp_path/svc/widget.py     (CWD-relative: caller is in svc/, passes widget.py)
        #   tmp_path/widget.py         (repo-root-relative: same path relative to root)
        svc_dir = tmp_path / "svc"
        svc_dir.mkdir()
        (svc_dir / "widget.py").write_text("# svc version")
        (tmp_path / "widget.py").write_text("# root version")

        argv = _argv(tmp_path, files=["widget.py"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] commit")
            return _make_completed(0)

        # Caller is inside svc/ — the CWD-relative path (svc/widget.py) should win
        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(svc_dir)):
            rc = mod.main(argv)

        assert rc == 0
        assert captured_add_args[0] == "--"
        # CWD-relative wins: svc/widget.py (repo-root-relative), not widget.py
        assert captured_add_args[1] == "svc/widget.py"


class TestSubdirectoryPathResolution:
    """Paths relative to a subdirectory CWD are resolved to repo-root-relative."""

    def test_paths_resolved_from_subdir_cwd(self, tmp_path):
        """git add receives repo-root-relative path when caller is in a subdirectory."""
        mod = _load_module()

        # Simulate a monorepo: repo root is tmp_path, caller CWD is tmp_path/apps/scraper
        subdir = tmp_path / "apps" / "scraper"
        subdir.mkdir(parents=True)
        test_file = subdir / "tests" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# test")

        # User passes path relative to their CWD inside the subdir
        argv = _argv(tmp_path, files=["tests/test_foo.py"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] commit")
            # tusk lint
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 0
        # Should have resolved to repo-root-relative: apps/scraper/tests/test_foo.py
        assert captured_add_args[0] == "--"
        assert captured_add_args[1] == os.path.join("apps", "scraper", "tests", "test_foo.py")

    def test_repo_root_relative_paths_unchanged(self, tmp_path):
        """Paths already relative to repo root (caller at repo root) pass through unchanged."""
        mod = _load_module()

        test_file = tmp_path / "src" / "foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("# src")

        argv = _argv(tmp_path, files=["src/foo.py"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] commit")
            return _make_completed(0)

        # Caller CWD == repo root (the common case)
        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert captured_add_args == ["--", "src/foo.py"]

    def test_missing_path_emits_clear_diagnostic(self, tmp_path, capsys):
        """When a resolved path does not exist, a clear diagnostic is printed and exit 3 returned."""
        mod = _load_module()

        subdir = tmp_path / "apps" / "scraper"
        subdir.mkdir(parents=True)

        # File does NOT exist
        argv = _argv(tmp_path, files=["tests/nonexistent.py"])

        def fake_run(args, **kwargs):
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(subdir)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "Error: path not found" in captured.err
        assert "tests/nonexistent.py" in captured.err

    def test_missing_path_errors_before_lint(self, tmp_path, capsys):
        """Missing path exits with code 3 before lint or tests are invoked (fail-fast)."""
        mod = _load_module()

        # File does NOT exist
        argv = _argv(tmp_path, files=["does_not_exist.py"])

        lint_called = []

        def fake_run(args, **kwargs):
            if "lint" in args:
                lint_called.append(args)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        assert lint_called == [], "lint must not be invoked when a file path is invalid"
        captured = capsys.readouterr()
        assert "Error: path not found" in captured.err

    def test_escape_errors_before_lint(self, tmp_path, capsys):
        """Path-escapes-repo-root error exits with code 3 before lint is invoked (fail-fast)."""
        mod = _load_module()

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        argv = _argv(repo_root, files=["../outside.py"])

        lint_called = []

        def fake_run(args, **kwargs):
            if "lint" in args:
                lint_called.append(args)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        assert lint_called == [], "lint must not be invoked when a path escapes the repo root"
        captured = capsys.readouterr()
        assert "Error: path escapes the repo root" in captured.err

    def test_absolute_paths_passed_through(self, tmp_path):
        """Absolute file paths are not modified."""
        mod = _load_module()

        abs_file = tmp_path / "some" / "abs.py"
        abs_file.parent.mkdir(parents=True)
        abs_file.write_text("# abs")

        argv = _argv(tmp_path, files=[str(abs_file)])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] commit")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert captured_add_args == ["--", str(abs_file)]

    def test_absolute_path_outside_repo_root_emits_diagnostic(self, tmp_path, capsys):
        """Absolute path outside the repo root emits the same 'path escapes' diagnostic as relative paths."""
        mod = _load_module()

        # repo root is a subdirectory; the file lives outside it
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside_file = tmp_path / "outside.py"
        outside_file.write_text("# outside")

        argv = _argv(repo_root, files=[str(outside_file)])

        def fake_run(args, **kwargs):
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(repo_root)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "Error: path escapes the repo root" in captured.err
        assert str(outside_file) in captured.err

    def test_path_escaping_repo_root_emits_diagnostic(self, tmp_path, capsys):
        """Path whose resolved absolute location is outside the repo root exits 3 with clear error."""
        mod = _load_module()

        # repo root is a subdirectory; caller CWD is its parent (outside repo root)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside_cwd = tmp_path  # parent of repo — outside the repo

        # A relative path that resolves to somewhere outside repo_root
        argv = _argv(repo_root, files=["../outside.py"])

        def fake_run(args, **kwargs):
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(outside_cwd)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "Error: path escapes the repo root" in captured.err
        assert "../outside.py" in captured.err


class TestMarkdownFileRegression:
    """Regression: .md files at repo-root-relative paths must be staged correctly (GitHub Issue #350)."""

    def test_md_file_staged_with_separator(self, tmp_path):
        """git add receives -- separator and repo-root-relative .md path."""
        mod = _load_module()

        doc_file = tmp_path / "apps" / "web" / "DEPLOYMENT.md"
        doc_file.parent.mkdir(parents=True)
        doc_file.write_text("# Deployment")

        argv = _argv(tmp_path, files=["apps/web/DEPLOYMENT.md"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                captured_add_args.extend(args[2:])
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc123\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main abc123] commit")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert captured_add_args[0] == "--"
        assert captured_add_args[1] == os.path.join("apps", "web", "DEPLOYMENT.md")

    def test_gitignore_rejection_emits_specific_hint(self, tmp_path, capsys):
        """When git add fails with a gitignore message, tusk emits a hint about -f."""
        mod = _load_module()

        doc_file = tmp_path / "README.md"
        doc_file.write_text("# Readme")

        argv = _argv(tmp_path, files=["README.md"])

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    1,
                    stderr="The following paths are ignored by one of your .gitignore files:\nREADME.md",
                )
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert ".gitignore" in captured.err
        assert "git add -f" in captured.err

    def test_git_add_failure_prints_command_and_cwd(self, tmp_path, capsys):
        """When git add fails, the exact command and cwd are printed for manual reproduction."""
        mod = _load_module()

        target = tmp_path / "apps" / "web" / "DEPLOYMENT.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Deploy")

        argv = _argv(tmp_path, files=["apps/web/DEPLOYMENT.md"])

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr="fatal: pathspec 'apps/web/DEPLOYMENT.md' did not match any files",
                )
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert str(tmp_path) in captured.err   # cwd printed
        assert "git add" in captured.err        # command printed
