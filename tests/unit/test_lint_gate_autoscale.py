"""Unit coverage for the pre-merge lint gate timeout auto-scale (issue #1070).

The gate reuses the commit test gate's p95 machinery: successful lint-gate
runs record elapsed-time samples under LINT_GATE_SAMPLE_KEY in test_runs, and
the timeout resolver inserts an auto layer between config and the 60s static
default. Machine-load spikes that stretch a normally-fast lint past 60s stop
aborting batch merges once one slow-but-successful run lands in history.
"""

import importlib.util
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
sys.path.insert(0, BIN)

_spec = importlib.util.spec_from_file_location(
    "tusk_merge", os.path.join(BIN, "tusk-merge.py")
)
merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge)


@pytest.fixture
def db_with_samples(tmp_path):
    def make(elapsed, count=20, succeeded=1):
        db = str(tmp_path / "tasks.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_runs ("
            "id INTEGER PRIMARY KEY, task_id INTEGER, test_command TEXT, "
            "elapsed_seconds REAL, succeeded INTEGER)"
        )
        conn.execute("DELETE FROM test_runs")
        for _ in range(count):
            conn.execute(
                "INSERT INTO test_runs (task_id, test_command, elapsed_seconds, succeeded) "
                "VALUES (1, ?, ?, ?)",
                (merge.LINT_GATE_SAMPLE_KEY, float(elapsed), succeeded),
            )
        conn.commit()
        conn.close()
        return db

    return make


class TestLoadLintTimeoutResolution:
    def test_default_without_db(self):
        assert merge.load_lint_timeout("/nonexistent/config.json") == (60, "default")

    def test_auto_scales_from_slow_history(self, db_with_samples):
        db = db_with_samples(80.0)
        assert merge.load_lint_timeout("/nonexistent/config.json", db) == (160, "auto")

    def test_fast_history_floors_at_static_default(self, db_with_samples):
        db = db_with_samples(3.0)
        assert merge.load_lint_timeout("/nonexistent/config.json", db) == (60, "auto")

    def test_cold_start_falls_through_to_default(self, db_with_samples):
        db = db_with_samples(80.0, count=5)
        assert merge.load_lint_timeout("/nonexistent/config.json", db) == (60, "default")

    def test_failed_runs_do_not_count(self, db_with_samples):
        db = db_with_samples(80.0, succeeded=0)
        assert merge.load_lint_timeout("/nonexistent/config.json", db) == (60, "default")

    def test_env_overrides_auto(self, db_with_samples, monkeypatch):
        db = db_with_samples(80.0)
        monkeypatch.setenv("TUSK_LINT_TIMEOUT", "240")
        assert merge.load_lint_timeout("/nonexistent/config.json", db) == (240, "env")

    def test_config_overrides_auto(self, db_with_samples, tmp_path):
        db = db_with_samples(80.0)
        config = tmp_path / "config.json"
        config.write_text('{"lint_timeout_sec": 90}', encoding="utf-8")
        assert merge.load_lint_timeout(str(config), db) == (90, "config")


class TestGateRecordsSamples:
    def test_successful_gate_run_records_sample(self, db_with_samples, tmp_path):
        db = db_with_samples(1.0, count=0)
        with patch.object(merge.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            rc = merge._run_pre_merge_lint(
                "tusk", "/nonexistent/config.json", 42, db_path=db
            )
        assert rc == 0
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT task_id, test_command, succeeded FROM test_runs"
        ).fetchone()
        conn.close()
        assert row[0] == 42
        assert row[1] == merge.LINT_GATE_SAMPLE_KEY
        assert row[2] == 1

    def test_failed_gate_run_records_unsuccessful_sample(self, db_with_samples):
        db = db_with_samples(1.0, count=0)
        with patch.object(merge.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=6, stdout="violation", stderr="")
            rc = merge._run_pre_merge_lint(
                "tusk", "/nonexistent/config.json", 42, db_path=db
            )
        assert rc == 6
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT succeeded FROM test_runs"
        ).fetchone()[0] == 0
        conn.close()

    def test_no_db_path_keeps_legacy_behavior(self):
        with patch.object(merge.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert merge._run_pre_merge_lint("tusk", "/nonexistent/config.json", 42) == 0


class TestTimeoutMessageNamesAutoSource:
    def test_auto_source_named_on_timeout(self, db_with_samples, capsys):
        db = db_with_samples(80.0)
        with patch.object(merge.subprocess, "run") as run:
            run.side_effect = merge.subprocess.TimeoutExpired(cmd="tusk lint", timeout=160)
            rc = merge._run_pre_merge_lint(
                "tusk", "/nonexistent/config.json", 42, db_path=db
            )
        assert rc == 8
        err = capsys.readouterr().err
        assert "timed out after 160s" in err
        assert "auto-scaled from p95 of recent successful lint-gate runs" in err
        assert "lint_timeout_sec" in err
