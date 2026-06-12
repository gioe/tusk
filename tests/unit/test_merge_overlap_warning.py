"""Unit tests for the main-side/branch file-overlap warning (issue #1081).

When origin/<default> has advanced with commits touching files this branch
also modified, tusk merge surfaces the overlap (files + main-side commit ids)
before rebasing or refusing the no-checkout push — converting a surprise
mid-merge conflict into an early heads-up. The warning is best-effort: any
git failure or an empty intersection produces no output.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    tusk_loader_mock.load.return_value = db_lib_mock
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _cp(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fake_run(main_files, branch_files, log_lines, base_rc=0, diff_rc=0):
    def run(args, check=True):
        if args[:2] == ["git", "merge-base"]:
            return _cp(base_rc, stdout="abc123\n")
        if args[:3] == ["git", "diff", "--name-only"]:
            target = args[3]
            files = main_files if "origin/" in target else branch_files
            return _cp(diff_rc, stdout="\n".join(files) + "\n")
        if args[:2] == ["git", "log"] and "--format=%h %s" in args:
            return _cp(0, stdout="\n".join(log_lines) + "\n")
        return _cp(0)

    return run


class TestMainSideOverlap:
    def test_overlap_returns_files_and_commits(self):
        mod = _load_module()
        fake = _fake_run(
            main_files=["src/scraper.py", "docs/README.md"],
            branch_files=["src/scraper.py", "src/new_feature.py"],
            log_lines=["aaa111 [TASK-2716] Add etix fallback routing"],
        )
        with patch.object(mod, "run", side_effect=fake):
            overlap, commits = mod._main_side_overlap("feature/TASK-1-x", "main")
        assert overlap == ["src/scraper.py"]
        assert commits == ["aaa111 [TASK-2716] Add etix fallback routing"]

    def test_no_overlap_returns_empty(self):
        mod = _load_module()
        fake = _fake_run(
            main_files=["docs/README.md"],
            branch_files=["src/new_feature.py"],
            log_lines=[],
        )
        with patch.object(mod, "run", side_effect=fake):
            overlap, commits = mod._main_side_overlap("feature/TASK-1-x", "main")
        assert overlap == []
        assert commits == []

    def test_merge_base_failure_returns_empty(self):
        mod = _load_module()
        fake = _fake_run([], [], [], base_rc=1)
        with patch.object(mod, "run", side_effect=fake):
            assert mod._main_side_overlap("feature/TASK-1-x", "main") == ([], [])

    def test_diff_failure_returns_empty(self):
        mod = _load_module()
        fake = _fake_run(["a"], ["a"], [], diff_rc=1)
        with patch.object(mod, "run", side_effect=fake):
            assert mod._main_side_overlap("feature/TASK-1-x", "main") == ([], [])


class TestFormatOverlapWarning:
    def test_names_files_and_commits(self):
        mod = _load_module()
        msg = mod._format_main_side_overlap_warning(
            "main",
            ["src/scraper.py"],
            ["aaa111 [TASK-2716] Add etix fallback routing"],
        )
        assert "origin/main has commits touching 1 file(s)" in msg
        assert "files: src/scraper.py" in msg
        assert "aaa111 [TASK-2716] Add etix fallback routing" in msg
        assert "semantic" in msg

    def test_caps_long_file_lists(self):
        mod = _load_module()
        files = [f"f{i}.py" for i in range(15)]
        msg = mod._format_main_side_overlap_warning("main", files, [])
        assert "... and 5 more" in msg
        assert "main-side commits:" not in msg


class TestMaybeWarn:
    def test_prints_warning_on_overlap(self, capsys):
        mod = _load_module()
        with patch.object(
            mod, "_main_side_overlap",
            return_value=(["src/a.py"], ["abc [TASK-9] change a"]),
        ):
            mod._maybe_warn_main_side_overlap("feature/TASK-1-x", "main")
        err = capsys.readouterr().err
        assert "this branch also modified" in err
        assert "abc [TASK-9] change a" in err

    def test_silent_when_no_overlap(self, capsys):
        mod = _load_module()
        with patch.object(mod, "_main_side_overlap", return_value=([], [])):
            mod._maybe_warn_main_side_overlap("feature/TASK-1-x", "main")
        assert capsys.readouterr().err == ""
