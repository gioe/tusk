"""Unit tests for the bimodal-timeout floor and progressing-timeout auto-retry (issue #1062).

Covers _compute_auto_timeout's max-recent floor (a warm-dominated history cannot
yield a ceiling below an already-observed cold run) and _run_test_with_retry's
gating (auto + progress retries once; env source and silent hangs do not).
"""

import importlib.util
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path) -> str:
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                session_id INTEGER,
                test_command TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL,
                succeeded INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


def _seed(db_path, command, samples):
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO test_runs (test_command, elapsed_seconds, succeeded) "
            "VALUES (?, ?, 1)",
            [(command, e) for e in samples],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Max-recent floor in _compute_auto_timeout
# ---------------------------------------------------------------------------


def test_max_recent_floor_lifts_ceiling_above_observed_cold_run(tmp_path):
    mod = _load_module()
    db = _make_db(tmp_path)
    # Bimodal: 19 warm runs at 90s + one cold 460s success. p95 is dominated by
    # the warm runs (90s → *2 = 180), which without the floor would yield 240
    # (static default) — below the 460s cold run that already succeeded.
    _seed(db, "pytest", [90.0] * 19 + [460.0])
    result = mod._compute_auto_timeout(db, "pytest")
    # max_recent_floor = ceil(460 * 1.5) = 690 wins over p95*2 (180) and 240.
    assert result == 690


def test_no_slow_run_uses_static_floor(tmp_path):
    mod = _load_module()
    db = _make_db(tmp_path)
    # All warm: p95*2 = 180, max_recent = ceil(90*1.5) = 135, both under the
    # 240 static floor → 240.
    _seed(db, "pytest", [90.0] * 20)
    assert mod._compute_auto_timeout(db, "pytest") == 240


def test_max_recent_floor_can_exceed_p95_scaled(tmp_path):
    mod = _load_module()
    db = _make_db(tmp_path)
    # 19 @ 200s + 1 @ 1000s. p95 = sorted[18] = 200 → *2 = 400.
    # max_recent = ceil(1000 * 1.5) = 1500 > 400 and > 240 → 1500.
    _seed(db, "pytest", [200.0] * 19 + [1000.0])
    assert mod._compute_auto_timeout(db, "pytest") == 1500


# ---------------------------------------------------------------------------
# _run_test_with_retry gating
# ---------------------------------------------------------------------------

# A command that prints immediately (progress), then sleeps long enough to blow
# a 1s ceiling but finish within the doubled (2s) retry ceiling.
_PROGRESS_THEN_SLOW = "printf 'start\\n'; sleep 1.5; printf 'done\\n'"
# Progress, then sleeps past even the retry ceiling.
_PROGRESS_THEN_HANG = "printf 'start\\n'; sleep 30"
# No output at all, then sleeps past the ceiling (silent hang).
_SILENT_HANG = "sleep 30"


def test_auto_progress_timeout_retries_and_passes(tmp_path, capsys):
    mod = _load_module()
    test, elapsed = mod._run_test_with_retry(
        _PROGRESS_THEN_SLOW, str(tmp_path), 1, "auto", verbose=False,
    )
    assert test is not None, "expected the widened retry to complete"
    assert test.returncode == 0
    assert "retrying once with a widened ceiling" in capsys.readouterr().err


def test_env_source_does_not_retry(tmp_path, capsys):
    mod = _load_module()
    # Same slow command, but an explicit env-source ceiling is respected as-is:
    # no retry, terminal timeout → (None, None).
    test, elapsed = mod._run_test_with_retry(
        _PROGRESS_THEN_SLOW, str(tmp_path), 1, "env", verbose=False,
    )
    assert test is None
    err = capsys.readouterr().err
    assert "retrying once" not in err
    assert "timed out after 1s" in err


def test_silent_hang_is_not_retried(tmp_path, capsys):
    mod = _load_module()
    test, elapsed = mod._run_test_with_retry(
        _SILENT_HANG, str(tmp_path), 1, "auto", verbose=False,
    )
    assert test is None
    assert "retrying once" not in capsys.readouterr().err


def test_retry_that_also_times_out_aborts(tmp_path, capsys):
    mod = _load_module()
    test, elapsed = mod._run_test_with_retry(
        _PROGRESS_THEN_HANG, str(tmp_path), 1, "auto", verbose=False,
    )
    assert test is None
    err = capsys.readouterr().err
    assert "retrying once with a widened ceiling" in err
    assert "timed out again" in err


def test_passing_command_returns_process(tmp_path):
    mod = _load_module()
    test, elapsed = mod._run_test_with_retry(
        "printf 'ok\\n'", str(tmp_path), 30, "auto", verbose=False,
    )
    assert test is not None
    assert test.returncode == 0
    assert elapsed is not None


def test_timeout_had_progress_signal():
    mod = _load_module()
    import subprocess
    assert mod._timeout_had_progress(
        subprocess.TimeoutExpired(cmd="x", timeout=1, output="partial", stderr="")
    ) is True
    assert mod._timeout_had_progress(
        subprocess.TimeoutExpired(cmd="x", timeout=1, output="", stderr="")
    ) is False
