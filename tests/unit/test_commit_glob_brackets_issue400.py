"""Regression test for GitHub Issue #400.

tusk commit should succeed when a file path contains square brackets
(e.g. apps/api/[id]/route.ts) that zsh would normally expand as a
glob pattern at the shell level.

At the Python level (tusk-commit.py), paths arrive already de-quoted
because the shell only expands globs if the user did NOT quote the path.
When the user correctly quotes the path (e.g. "apps/api/[id]/route.ts"),
the shell passes the literal string — including the brackets — to tusk,
and tusk-commit.py must forward that literal path to `git add` unchanged.

These tests confirm:
  - _make_relative handles paths whose components contain brackets
  - The full commit flow passes the literal bracket path to git add
  - The "path not found" error includes a glob-quoting hint when the
    missing path contains bracket characters
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


class TestMakeRelativeSquareBrackets:
    """_make_relative must preserve square brackets in path components."""

    def test_bracket_path_component_preserved(self, tmp_path):
        mod = _load_module()
        repo_root = str(tmp_path)
        abs_path = os.path.join(repo_root, "apps", "api", "[id]", "route.ts")
        result = mod._make_relative(abs_path, repo_root)
        assert "[id]" in result
        assert not result.startswith("..")

    def test_multiple_bracket_components_preserved(self, tmp_path):
        mod = _load_module()
        repo_root = str(tmp_path)
        abs_path = os.path.join(repo_root, "apps", "[locale]", "[id]", "page.tsx")
        result = mod._make_relative(abs_path, repo_root)
        assert "[locale]" in result
        assert "[id]" in result
        assert not result.startswith("..")


class TestCommitBracketPath:
    """Full-flow test: tusk commit passes literal bracket paths to git add."""

    def test_bracket_path_passed_to_git_add_unchanged(self, tmp_path, capsys):
        mod = _load_module()

        # Create the file at a path with a square-bracket directory component.
        target = tmp_path / "apps" / "api" / "[id]" / "route.ts"
        target.parent.mkdir(parents=True)
        target.write_text("export default function handler() {}")

        config = tmp_path / "config.json"
        config.write_text("{}")

        # Path as the user would supply it after the shell has de-quoted it
        # (i.e. the literal string including brackets).
        path_arg = "apps/api/[id]/route.ts"
        argv = [str(tmp_path), str(config), "400", "add handler", path_arg]

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
        # git add must receive the literal bracket path, not a glob-expanded form.
        assert captured_add_args[0] == "--"
        git_add_path = captured_add_args[1]
        assert "[id]" in git_add_path, (
            f"git add received '{git_add_path}' — square brackets were lost or expanded"
        )

    def test_path_not_found_with_brackets_shows_quoting_hint(self, tmp_path, capsys):
        """When a bracket path is missing, the error suggests quoting it."""
        mod = _load_module()

        config = tmp_path / "config.json"
        config.write_text("{}")

        # File does NOT exist — this will trigger the "path not found" error.
        argv = [str(tmp_path), str(config), "400", "add handler",
                "apps/api/[id]/route.ts"]

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "ls-files"]:
                return _make_completed(0, stdout="")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3
        err = capsys.readouterr().err
        assert "glob" in err.lower() or "quote" in err.lower() or "[" in err, (
            f"Expected a glob/quoting hint in error output, got:\n{err}"
        )
