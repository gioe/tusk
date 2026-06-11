"""Regression tests for issue #1074: argparse prefix abbreviation.

``tusk task-insert ... --type bug`` used to be silently expanded by argparse's
``allow_abbrev`` default to ``--typed-criteria``, whose JSON type-callable then
rejected the value with a misleading "must be valid JSON" error. Every tusk
parser is now constructed with ``allow_abbrev=False`` so a wrong flag fails as
"unrecognized arguments", which names the flag the caller actually typed.
"""

import ast
import glob
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TASK_INSERT = os.path.join(REPO_ROOT, "bin", "tusk-task-insert.py")


class TestTaskInsertWrongFlag:
    def _run(self, *extra_args):
        # argparse rejects the unknown flag before db/config are ever opened,
        # so nonexistent paths are safe placeholders here.
        return subprocess.run(
            [sys.executable, TASK_INSERT, "/nonexistent/tasks.db",
             "/nonexistent/config.json", "demo summary", "demo description",
             *extra_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_type_flag_reports_unrecognized_argument(self):
        result = self._run("--type", "bug")
        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr
        assert "--type" in result.stderr

    def test_type_flag_no_longer_maps_to_typed_criteria(self):
        result = self._run("--type", "bug")
        assert "must be valid JSON" not in result.stderr

    def test_full_flag_still_parses(self):
        # --task-type passes argparse; the failure (if any) happens later at
        # the DB layer, so stderr must not contain an argparse usage error.
        result = self._run("--task-type", "bug")
        assert "unrecognized arguments" not in result.stderr


class TestAllParsersDisableAbbreviation:
    """Drift guard: every argparse.ArgumentParser(...) and .add_parser(...)
    call in bin/*.py must pass allow_abbrev=False explicitly — subparsers do
    not inherit the setting from their parent parser."""

    def _parser_calls_missing_allow_abbrev(self, path):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            is_parser_ctor = (
                func.attr == "ArgumentParser"
                and isinstance(func.value, ast.Name)
                and func.value.id == "argparse"
            )
            if not (is_parser_ctor or func.attr == "add_parser"):
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if "allow_abbrev" not in kwargs:
                missing.append(f"{path}:{node.lineno}: {func.attr}")
        return missing

    def test_every_bin_parser_passes_allow_abbrev(self):
        missing = []
        for path in sorted(glob.glob(os.path.join(REPO_ROOT, "bin", "*.py"))):
            missing.extend(self._parser_calls_missing_allow_abbrev(path))
        assert not missing, (
            "Parsers constructed without allow_abbrev=False (issue #1074):\n"
            + "\n".join(missing)
        )
