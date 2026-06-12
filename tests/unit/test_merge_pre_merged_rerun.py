"""Unit tests for the pre-merged auto-complete re-run path (issue #1066).

Re-running tusk merge on a fully-finalized task (pushed, session closed, task
Done/completed) must exit 0 with an "already finalized" report instead of
tripping over each already-complete step: benign stale-main push rejection,
already-closed session warning, and an already-Done task-done error that
previously propagated as exit 2.
"""

import importlib.util
import json
import os
import sqlite3
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


def _last_json(capsys_out: str) -> dict:
    lines = [ln for ln in capsys_out.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


class TestTaskDoneRefusedAlreadyDone:
    def test_true_on_exit2_already_done(self):
        mod = _load_module()
        result = _cp(2, stderr="Error: Task 42 is already Done")
        assert mod._task_done_refused_already_done(result) is True

    def test_false_on_other_exit2_error(self):
        mod = _load_module()
        result = _cp(2, stderr="Error: Task 42 not found")
        assert mod._task_done_refused_already_done(result) is False

    def test_false_on_success(self):
        mod = _load_module()
        assert mod._task_done_refused_already_done(_cp(0)) is False


class TestTaskAlreadyFinalized:
    def _db_conn(self, status, closed_reason):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT, closed_reason TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks (id, status, closed_reason) VALUES (42, ?, ?)",
            (status, closed_reason),
        )
        conn.commit()
        # _task_already_finalized closes the connection; in-memory DBs vanish
        # on close, so hand out a wrapper that ignores close().
        class _NoClose:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def close(self):
                pass

        return _NoClose(conn)

    def test_done_completed_is_finalized(self):
        mod = _load_module()
        conn = self._db_conn("Done", "completed")
        with patch.object(mod, "get_connection", return_value=conn):
            assert mod._task_already_finalized("ignored", 42) is True

    def test_done_wont_do_is_not_finalized(self):
        mod = _load_module()
        conn = self._db_conn("Done", "wont_do")
        with patch.object(mod, "get_connection", return_value=conn):
            assert mod._task_already_finalized("ignored", 42) is False

    def test_in_progress_is_not_finalized(self):
        mod = _load_module()
        conn = self._db_conn("In Progress", None)
        with patch.object(mod, "get_connection", return_value=conn):
            assert mod._task_already_finalized("ignored", 42) is False

    def test_missing_row_is_not_finalized(self):
        mod = _load_module()
        conn = self._db_conn("Done", "completed")
        with patch.object(mod, "get_connection", return_value=conn):
            assert mod._task_already_finalized("ignored", 999) is False

    def test_sqlite_error_is_not_finalized(self):
        mod = _load_module()
        with patch.object(mod, "get_connection", side_effect=sqlite3.OperationalError("boom")):
            assert mod._task_already_finalized("ignored", 42) is False


class TestFormatPreMergedPushWarning:
    def test_non_fast_forward_gets_explanatory_note(self):
        mod = _load_module()
        stderr = (
            " ! [rejected]        main -> main (non-fast-forward)\n"
            "error: failed to push some refs"
        )
        msg = mod._format_pre_merged_push_warning("main", stderr)
        assert msg.startswith("Note:")
        assert "behind origin/main" in msg
        assert "Benign" in msg

    def test_fetch_first_gets_explanatory_note(self):
        mod = _load_module()
        msg = mod._format_pre_merged_push_warning("main", "! [rejected] (fetch first)")
        assert msg.startswith("Note:")

    def test_other_failure_keeps_generic_warning(self):
        mod = _load_module()
        msg = mod._format_pre_merged_push_warning("main", "fatal: unable to access remote")
        assert msg.startswith("Warning:")
        assert "may already be pushed" in msg


class TestAutoCompletePreMergedRerun:
    """Flow tests for _auto_complete_pre_merged."""

    def _common_patches(self, mod):
        return [
            patch.object(mod, "detect_default_branch", return_value="main"),
            patch.object(mod, "checkpoint_wal"),
            patch.object(mod, "_guard_no_open_completion_criteria", return_value=0),
            patch.object(mod, "_run_pre_merge_lint", return_value=0),
            # The module-level dumps comes from the mocked tusk_loader db lib;
            # swap in the real json.dumps so stdout assertions can parse it.
            patch.object(mod, "dumps", json.dumps),
            patch.object(mod, "_has_remote", return_value=True),
        ]

    def test_already_finalized_short_circuits_with_exit_0(self, capsys):
        mod = _load_module()
        git_calls = []

        def fake_run(args, check=True):
            git_calls.append(args)
            return _cp(0)

        subcommands = []

        def fake_subcommand(tusk_bin, args):
            subcommands.append(args)
            return _cp(2, stderr="Error: Session 7 is already closed")

        patches = self._common_patches(mod)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(mod, "_task_already_finalized", return_value=True), \
                patch.object(mod, "run", side_effect=fake_run), \
                patch.object(mod, "_run_tusk_subcommand", side_effect=fake_subcommand):
            rc = mod._auto_complete_pre_merged("tusk", "cfg", "db", 42, 7, False)

        captured = capsys.readouterr()
        assert rc == 0
        # No push attempted, no task-done invoked — only the session close.
        assert git_calls == []
        assert subcommands == [["session-close", "7"]]
        assert "already finalized — nothing to do" in captured.err
        payload = _last_json(captured.out)
        assert payload["already_finalized"] is True
        assert payload["task"] == {"id": 42, "status": "Done", "closed_reason": "completed"}
        assert payload["sessions_closed"] == 0

    def test_task_done_already_done_race_treated_as_success(self, capsys):
        mod = _load_module()

        def fake_subcommand(tusk_bin, args):
            if args[0] == "session-close":
                return _cp(0)
            if args[0] == "task-done":
                return _cp(2, stderr="Error: Task 42 is already Done")
            return _cp(0)

        patches = self._common_patches(mod)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(mod, "_task_already_finalized", side_effect=[False, True]), \
                patch.object(mod, "run", return_value=_cp(0)), \
                patch.object(mod, "_run_tusk_subcommand", side_effect=fake_subcommand):
            rc = mod._auto_complete_pre_merged("tusk", "cfg", "db", 42, 7, False)

        captured = capsys.readouterr()
        assert rc == 0
        payload = _last_json(captured.out)
        assert payload["already_finalized"] is True
        assert payload["sessions_closed"] == 1

    def test_genuine_task_done_failure_still_exits_2(self, capsys):
        mod = _load_module()

        def fake_subcommand(tusk_bin, args):
            if args[0] == "session-close":
                return _cp(0)
            if args[0] == "task-done":
                return _cp(2, stderr="Error: Task 42 not found")
            return _cp(0)

        patches = self._common_patches(mod)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(mod, "_task_already_finalized", return_value=False), \
                patch.object(mod, "run", return_value=_cp(0)), \
                patch.object(mod, "_run_tusk_subcommand", side_effect=fake_subcommand):
            rc = mod._auto_complete_pre_merged("tusk", "cfg", "db", 42, 7, False)

        captured = capsys.readouterr()
        assert rc == 2
        assert "task-done failed" in captured.err

    def test_first_time_finalization_unchanged(self, capsys):
        mod = _load_module()
        task_done_json = json.dumps(
            {"task_id": 42, "summary": "t", "unblocked_tasks": []}
        )

        def fake_subcommand(tusk_bin, args):
            if args[0] == "session-close":
                return _cp(0)
            if args[0] == "task-done":
                return _cp(0, stdout=task_done_json)
            return _cp(0)

        push_calls = []

        def fake_run(args, check=True):
            push_calls.append(args)
            return _cp(0)

        patches = self._common_patches(mod)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(mod, "_task_already_finalized", return_value=False), \
                patch.object(mod, "run", side_effect=fake_run), \
                patch.object(mod, "_run_tusk_subcommand", side_effect=fake_subcommand):
            rc = mod._auto_complete_pre_merged("tusk", "cfg", "db", 42, 7, False)

        captured = capsys.readouterr()
        assert rc == 0
        assert ["git", "push", "origin", "main"] in push_calls
        payload = _last_json(captured.out)
        assert payload["sessions_closed"] == 1
        assert "already_finalized" not in payload

    def test_stale_main_push_rejection_prints_note_not_warning(self, capsys):
        mod = _load_module()

        def fake_subcommand(tusk_bin, args):
            if args[0] == "task-done":
                return _cp(0, stdout=json.dumps({"task_id": 42, "unblocked_tasks": []}))
            return _cp(0)

        def fake_run(args, check=True):
            if args[:2] == ["git", "push"]:
                return _cp(1, stderr="! [rejected] main -> main (non-fast-forward)")
            return _cp(0)

        patches = self._common_patches(mod)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch.object(mod, "_task_already_finalized", return_value=False), \
                patch.object(mod, "run", side_effect=fake_run), \
                patch.object(mod, "_run_tusk_subcommand", side_effect=fake_subcommand):
            rc = mod._auto_complete_pre_merged("tusk", "cfg", "db", 42, 7, False)

        captured = capsys.readouterr()
        assert rc == 0
        assert "Note: git push origin main was rejected (non-fast-forward)" in captured.err
        assert "Warning: git push origin main failed" not in captured.err
