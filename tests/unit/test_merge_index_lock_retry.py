"""Unit tests for _checkout_with_index_lock_retry (Issue #620).

When `git checkout <default_branch>` fails with a transient
`Unable to create '.../.git/index.lock'` error, tusk-merge.py should sleep
briefly and retry once before surfacing the error. Other checkout failures
must surface immediately with no retry.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

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


_LOCK_STDERR = (
    "fatal: Unable to create '/tmp/repo/.git/index.lock': File exists.\n"
    "\nAnother git process seems to be running in this repository...\n"
)


class TestCheckoutWithIndexLockRetry:
    def test_first_try_success_no_retry_no_sleep(self, capsys):
        mod = _load_module()
        calls = []

        def fake_run(args, check=True):
            calls.append(args)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod.time, "sleep"
        ) as fake_sleep:
            result = mod._checkout_with_index_lock_retry("main")

        assert result.returncode == 0
        assert calls == [["git", "checkout", "main"]]
        fake_sleep.assert_not_called()
        # Happy path: no retry log line on stderr
        assert "transient" not in capsys.readouterr().err

    def test_lock_failure_then_success_retries_with_sleep(self, capsys):
        mod = _load_module()
        attempts = []

        def fake_run(args, check=True):
            attempts.append(args)
            if len(attempts) == 1:
                return _cp(128, stderr=_LOCK_STDERR)
            return _cp(0)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod.time, "sleep"
        ) as fake_sleep:
            result = mod._checkout_with_index_lock_retry("main")

        assert result.returncode == 0
        assert len(attempts) == 2
        assert attempts[0] == ["git", "checkout", "main"]
        assert attempts[1] == ["git", "checkout", "main"]
        fake_sleep.assert_called_once_with(0.5)
        captured = capsys.readouterr()
        assert "transient .git/index.lock contention" in captured.err
        assert "retrying once" in captured.err

    def test_lock_failure_twice_returns_second_error_verbatim(self, capsys):
        mod = _load_module()
        second_stderr = "fatal: Unable to create '/x/.git/index.lock': File exists.\nstill held\n"
        attempts = []

        def fake_run(args, check=True):
            attempts.append(args)
            if len(attempts) == 1:
                return _cp(128, stderr=_LOCK_STDERR)
            return _cp(128, stderr=second_stderr)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(mod.time, "sleep"):
            result = mod._checkout_with_index_lock_retry("main")

        assert result.returncode == 128
        # Caller surfaces result.stderr verbatim — must be the second attempt's stderr
        assert result.stderr == second_stderr
        assert len(attempts) == 2

    def test_non_lock_failure_no_retry_no_sleep(self, capsys):
        mod = _load_module()
        non_lock_stderr = "error: pathspec 'main' did not match any file(s) known to git\n"
        attempts = []

        def fake_run(args, check=True):
            attempts.append(args)
            return _cp(1, stderr=non_lock_stderr)

        with patch.object(mod, "run", side_effect=fake_run), patch.object(
            mod.time, "sleep"
        ) as fake_sleep:
            result = mod._checkout_with_index_lock_retry("main")

        assert result.returncode == 1
        assert result.stderr == non_lock_stderr
        assert len(attempts) == 1
        fake_sleep.assert_not_called()
        assert "transient" not in capsys.readouterr().err

    def test_sleep_seconds_override(self):
        mod = _load_module()
        responses = [_cp(128, stderr=_LOCK_STDERR), _cp(0)]

        with patch.object(mod, "run", side_effect=responses), patch.object(
            mod.time, "sleep"
        ) as fake_sleep:
            mod._checkout_with_index_lock_retry("main", sleep_seconds=0.01)

        fake_sleep.assert_called_once_with(0.01)
