"""Unit tests for tusk-commit.py subdirectory path resolution.

Verifies that file paths relative to the caller's CWD (e.g. inside a
monorepo subdirectory) are resolved to repo-root-relative paths before
being passed to `git add`, fixing the pathspec error described in
GitHub Issue #336.
"""

import importlib.util
import os
import subprocess
import sys
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


class TestCaseInsensitiveFsRegression:
    """Regression: macOS case-insensitive filesystem path mismatch must not trigger path-escapes error (GitHub Issues #354, #357)."""

    def test_commit_succeeds_when_repo_root_is_symlink_to_cwd(self, tmp_path, capsys):
        """tusk commit succeeds when repo_root is a symlink whose realpath differs from the passed root.

        This exercises the same realpath-normalisation fix that resolves the macOS case-insensitive
        path mismatch: without realpath, os.path.relpath(abs_path, repo_root) starts with '..'
        even for valid paths.  Works on all platforms (symlinks are universally supported).
        """
        mod = _load_module()

        real_root = tmp_path / "real_repo"
        real_root.mkdir()
        target_file = real_root / "file.py"
        target_file.write_text("# file")

        # sym_repo → real_repo; git may resolve to real_repo while CWD is under real_root.
        sym_root = tmp_path / "sym_repo"
        sym_root.symlink_to(real_root)

        # repo_root is the symlink; CWD is the real directory.
        argv = _argv(sym_root, files=["file.py"])

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
             patch("os.getcwd", return_value=str(real_root)):
            rc = mod.main(argv)

        # The escape check must not trigger — rc 0 is the key assertion.
        assert rc == 0, capsys.readouterr().err
        assert captured_add_args[0] == "--"
        # TASK-628 normalisation: relpath from a symlinked root must not yield '..' components.
        assert ".." not in captured_add_args[1], (
            f"git add received a path with '..' components: {captured_add_args[1]!r}"
        )
        assert captured_add_args[1] == "file.py"

    @pytest.mark.skipif(sys.platform != "darwin", reason="tests macOS case-insensitive FS logic")
    def test_commit_succeeds_when_repo_root_capitalization_differs(self, tmp_path, capsys):
        """tusk commit succeeds when git's repo root path differs in case from the CWD (macOS scenario).

        On real macOS, os.path.realpath does NOT canonicalize case — it only resolves
        symlinks.  The fix in _escapes_root() uses .lower() on both paths when
        sys.platform == 'darwin' so that paths differing only in case are treated as
        equivalent, without relying on realpath to normalize them.
        """
        mod = _load_module()

        target_file = tmp_path / "file.py"
        target_file.write_text("# file")

        # Simulate macOS: argv[0] (repo_root) is lowercase (as git may return it).
        # Unlike the previous version of this test, we do NOT mock realpath to
        # canonicalize case — real macOS realpath does not do that.
        lowercase_root = str(tmp_path).lower()
        argv = _argv(tmp_path, files=["file.py"])
        argv[0] = lowercase_root  # override repo_root with lowercase variant

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

        assert rc == 0, capsys.readouterr().err
        assert captured_add_args[0] == "--"
        assert "file.py" in captured_add_args[1]

    @pytest.mark.skipif(sys.platform != "darwin", reason="tests macOS case-insensitive FS logic")
    def test_commit_succeeds_for_absolute_path_with_wrong_case_on_case_insensitive_fs(self, tmp_path, capsys):
        """tusk commit succeeds when an absolute file path has wrong-case components (macOS scenario).

        Regression: realpath was only called when os.path.exists() returned True.  On a
        case-insensitive filesystem an absolute path with wrong-case directory components
        (e.g. /users/Repo/file.py vs /Users/Repo/file.py) passes os.path.exists (because
        macOS FS ignores case) but os.path.relpath does a byte-exact string comparison, so
        the escape check fired a false positive.

        Now that _escapes_root() handles case-folding directly on Darwin, we rely on real
        macOS behaviour rather than simulating canonicalization via a fake_realpath mock
        (real macOS realpath does not canonicalize case).
        """
        mod = _load_module()

        canonical_root = str(tmp_path)
        lowercase_root = canonical_root.lower()
        # Absolute path to a file using wrong-case root (as macOS FS would accept).
        wrong_case_file = lowercase_root + "/file.py"
        # Create the actual file so the path is real on disk.
        (tmp_path / "file.py").write_text("# file")

        argv = _argv(tmp_path, files=[wrong_case_file])
        # argv[0] is canonical repo_root — only the file path itself has wrong case.

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
             patch("os.getcwd", return_value=canonical_root):
            rc = mod.main(argv)

        assert rc == 0, capsys.readouterr().err
        assert captured_add_args[0] == "--"

    def test_genuine_escape_still_rejected_after_realpath(self, tmp_path, capsys):
        """Paths that genuinely escape the repo root are rejected even after realpath normalization."""
        mod = _load_module()

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

    @pytest.mark.skipif(sys.platform != "darwin", reason="tests macOS case-insensitive FS logic")
    def test_commit_issue_357_cwd_uppercase_root_lowercase(self, tmp_path, capsys):
        """Regression for GitHub Issue #357: tusk commit must not reject valid paths when the
        stored repo root uses a different case than the CWD path (e.g. desktop vs Desktop).

        Exact scenario from the report:
          - repo_root stored as: /Users/mattgioe/desktop/projects/laughtrack  (lowercase d)
          - caller CWD:          /Users/mattgioe/Desktop/projects/laughtrack/apps/web
          - file passed:         apps/web/ui/components/modals/basic/index.tsx

        On macOS, os.path.realpath does NOT canonicalize case, so the repo root stays
        lowercase while real_abs resolves to the canonical-case path.  The escape check
        must use case-insensitive comparison (_escapes_root) rather than relying on
        realpath to normalize the case difference.
        """
        mod = _load_module()

        # Set up a real subdirectory structure under tmp_path (canonical case).
        subdir = tmp_path / "apps" / "web" / "ui"
        subdir.mkdir(parents=True)
        target_file = subdir / "index.tsx"
        target_file.write_text("// component")

        canonical_root = str(tmp_path)
        lowercase_root = canonical_root.lower()

        # argv[0] is the lowercase root (as git may return on macOS with case mismatch).
        # CWD is the canonical-case subdirectory.
        caller_cwd = str(tmp_path / "apps" / "web")
        argv = _argv(tmp_path, files=["ui/index.tsx"])
        argv[0] = lowercase_root

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
             patch("os.getcwd", return_value=caller_cwd):
            rc = mod.main(argv)

        assert rc == 0, capsys.readouterr().err


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


class TestDotDirectoryPathsRegression:
    """Regression: .github/ and other dot-directory paths must be staged correctly (GitHub Issue #348)."""

    def test_github_workflow_path_staged_with_separator(self, tmp_path):
        """git add receives -- separator and repo-root-relative .github/workflows/*.yml path."""
        mod = _load_module()

        workflow_file = tmp_path / ".github" / "workflows" / "ci.yml"
        workflow_file.parent.mkdir(parents=True)
        workflow_file.write_text("name: CI")

        argv = _argv(tmp_path, files=[".github/workflows/ci.yml"])

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
        assert captured_add_args[1] == os.path.join(".github", "workflows", "ci.yml")

    def test_circleci_path_staged_with_separator(self, tmp_path):
        """git add receives -- separator and repo-root-relative .circleci/ path."""
        mod = _load_module()

        config_file = tmp_path / ".circleci" / "config.yml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text("version: 2.1")

        argv = _argv(tmp_path, files=[".circleci/config.yml"])

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
        assert captured_add_args[1] == os.path.join(".circleci", "config.yml")

    def test_gitignored_github_path_reveals_specific_rule(self, tmp_path, capsys):
        """When a .github/ file is gitignored, git check-ignore -v identifies the matching rule."""
        mod = _load_module()

        workflow_file = tmp_path / ".github" / "workflows" / "ci.yml"
        workflow_file.parent.mkdir(parents=True)
        workflow_file.write_text("name: CI")

        argv = _argv(tmp_path, files=[".github/workflows/ci.yml"])

        gitignore_rule = ".gitignore:1:.github/\t.github/workflows/ci.yml"

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    128,
                    stderr="fatal: pathspec '.github/workflows/ci.yml' did not match any files",
                )
            if args[:3] == ["git", "check-ignore", "-v"]:
                return _make_completed(0, stdout=gitignore_rule + "\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert ".gitignore:1:.github/" in captured.err
        assert "git add -f" in captured.err

    def test_sparse_checkout_error_shows_hint(self, tmp_path, capsys):
        """When git add fails due to sparse-checkout, an actionable hint is shown."""
        mod = _load_module()

        workflow_file = tmp_path / ".github" / "workflows" / "ci.yml"
        workflow_file.parent.mkdir(parents=True)
        workflow_file.write_text("name: CI")

        argv = _argv(tmp_path, files=[".github/workflows/ci.yml"])

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(
                    1,
                    stderr=(
                        "error: the following pathspecs are outside the sparse-checkout "
                        "definition:\n  .github/workflows/ci.yml"
                    ),
                )
            if args[:3] == ["git", "check-ignore", "-v"]:
                return _make_completed(1)  # not gitignored
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "sparse-checkout" in captured.err
        assert "git sparse-checkout add" in captured.err


class TestDotDotPreflightRejection:
    """Resolved paths containing '..' are caught at pre-flight, not at git add (TASK-628).

    The belt-and-suspenders guard fires when the normalised resolved path (produced via
    os.path.relpath(real_abs, real_repo_root)) still contains '..' — meaning real_abs
    genuinely escapes real_repo_root but the escape check was somehow bypassed.

    We simulate that scenario by:
      1. Placing a file OUTSIDE repo_root so real_abs genuinely escapes.
      2. Patching _escapes_root to return False so the first-layer check is bypassed.
      3. Verifying that the second-layer (the '..' guard) rejects the path before git add.
    """

    def test_dotdot_guard_fires_when_escape_check_bypassed(self, tmp_path, capsys):
        """Pre-flight rejects a path with '..' when real_abs escapes and escape check is bypassed."""
        mod = _load_module()

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # The file lives OUTSIDE repo_root — real_abs genuinely escapes.
        outside_file = tmp_path / "outside.py"
        outside_file.write_text("# outside")

        # Pass '../outside.py': from CWD=repo_root this resolves to tmp_path/outside.py.
        argv = _argv(repo_root, files=["../outside.py"])

        git_add_called = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                git_add_called.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        # First layer (escape check) is bypassed; second layer (..' guard) must catch it.
        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(repo_root)), \
             patch.object(mod, "_escapes_root", return_value=False):
            rc = mod.main(argv)

        assert rc == 3
        assert git_add_called == [], "git add must not be called when a '..' path slips through"
        captured = capsys.readouterr()
        assert "'..' components" in captured.err

    def test_dotdot_guard_diagnostic_mentions_original_path(self, tmp_path, capsys):
        """The '..' guard error message includes the original path so the user knows what to fix."""
        mod = _load_module()

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside_file = tmp_path / "outside.py"
        outside_file.write_text("# outside")

        argv = _argv(repo_root, files=["../outside.py"])

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(repo_root)), \
             patch.object(mod, "_escapes_root", return_value=False):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "../outside.py" in captured.err  # original path is surfaced


class TestDeletedFilePathRegression:
    """Regression for GitHub Issue #375: tusk commit must accept git-tracked deleted file paths.

    The pre-flight validation previously used os.path.exists() alone, which returns False
    for deleted files and caused exit code 3 ('path not found') even though git add handles
    deletions natively.  The fix runs git ls-files to distinguish valid deletions (tracked
    by git) from truly missing files (not on disk and not in the git index).
    """

    def test_deleted_git_tracked_file_proceeds(self, tmp_path):
        """A file deleted from disk but tracked by git passes pre-flight and reaches git add."""
        mod = _load_module()

        # File does NOT exist on disk (simulates a deletion)
        argv = _argv(tmp_path, files=["deleted_module.py"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "ls-files"]:
                # Report the file as git-tracked (it was deleted but git knows about it)
                return _make_completed(0, stdout="deleted_module.py\n")
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
        assert "deleted_module.py" in captured_add_args[1]

    def test_deleted_untracked_file_still_errors(self, tmp_path, capsys):
        """A file that is neither on disk nor tracked by git still produces exit code 3."""
        mod = _load_module()

        # File does NOT exist on disk and is NOT git-tracked
        argv = _argv(tmp_path, files=["truly_missing.py"])

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "ls-files"]:
                # Empty output: file is not tracked
                return _make_completed(0, stdout="")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        captured = capsys.readouterr()
        assert "Error: path not found" in captured.err
        assert "truly_missing.py" in captured.err

    def test_mix_of_deleted_and_existing_files(self, tmp_path):
        """A deleted (git-tracked) file and an existing modified file can be committed together."""
        mod = _load_module()

        # One file exists, one is deleted (not on disk but git-tracked)
        existing = tmp_path / "kept.py"
        existing.write_text("# kept")

        argv = _argv(tmp_path, files=["deleted_module.py", "kept.py"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "ls-files"]:
                # Only the deleted file is reported as tracked
                return _make_completed(0, stdout="deleted_module.py\n")
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
        assert "--" in captured_add_args
        staged = set(captured_add_args) - {"--"}
        assert any("deleted_module.py" in p for p in staged)
        assert any("kept.py" in p for p in staged)

    def test_deleted_file_git_ls_files_not_called_when_all_exist(self, tmp_path):
        """git ls-files is NOT called when all specified files exist on disk (no-op fast path)."""
        mod = _load_module()

        existing = tmp_path / "somefile.py"
        existing.write_text("# exists")

        argv = _argv(tmp_path, files=["somefile.py"])

        ls_files_called = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "ls-files"]:
                ls_files_called.append(args)
                return _make_completed(0, stdout="somefile.py\n")
            if args[:2] == ["git", "add"]:
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
        assert ls_files_called == [], "git ls-files must not run when all files exist on disk"
