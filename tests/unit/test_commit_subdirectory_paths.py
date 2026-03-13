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
        assert len(captured_add_args) == 1
        assert captured_add_args[0] == os.path.join("svc", "app", "foo.py")


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
        assert len(captured_add_args) == 1
        assert captured_add_args[0] == os.path.join("apps", "scraper", "tests", "test_foo.py")

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
        assert captured_add_args == ["src/foo.py"]

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
        assert captured_add_args == [str(abs_file)]

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
