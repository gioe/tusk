"""Unit tests for test_command timeout behavior in tusk-commit.py (Issue #483).

Covers:
  - load_test_command_timeout resolution (env var > config > auto > default)
  - Fallback behavior for invalid values (negative, zero, non-numeric, missing)
  - Auto-scale layer (Issue #575 / TASK-192): once enough successful test_runs
    rows exist for a given test_command, the resolver returns
    max(DEFAULT_TEST_COMMAND_TIMEOUT_SEC, ceil(p95 * 2)). Cold-starts (fewer
    samples than the configured threshold) fall through to the static default.
  - main() returns exit code 5 when the test_command subprocess raises
    subprocess.TimeoutExpired, with a message that names the configured timeout
    and the source hint (config key or env var).
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_config(tmp_path, data: dict) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return str(p)


class TestLoadTestCommandTimeout:
    def test_default_when_nothing_set(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"

    def test_default_value_is_240_seconds(self):
        # Pins the static default value (Issue #575). Tusk's own unit suite
        # runs in ~53–57s; consumer suites in the 60–110s band were hitting
        # the prior 120s ceiling under load. 240s gives ~2x headroom on
        # typical runs while keeping the worst-case wait on a hung suite to
        # under 5 minutes.
        mod = _load_module()
        assert mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC == 240

    def test_config_value_used_when_env_unset(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "30")
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 30
        assert source == "env"

    def test_invalid_env_falls_back_to_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "not-a-number")
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_zero_or_negative_env_falls_through(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "0")
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_invalid_config_falls_back_to_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": -5})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"

    def test_missing_config_file_returns_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = str(tmp_path / "nonexistent.json")
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"


class TestTimeoutPath:
    """Verify main() returns exit code 5 when test_command hits the timeout."""

    def _make_repo(self, tmp_path):
        """Create a throwaway git repo with a single tracked file to commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@t"], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "t"], check=True
        )
        # Seed an initial commit so HEAD exists for the pre-commit SHA probe.
        seed = repo / "seed.txt"
        seed.write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True
        )
        target = repo / "file.txt"
        target.write_text("change\n")
        return repo, target

    def test_timeout_returns_exit_5_with_message(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        # Stub the lint subprocess and the criteria-done subprocess (no criteria
        # here, so only lint is invoked via tusk_bin).  The test_cmd call runs
        # through the real subprocess.run — which we monkeypatch to raise
        # TimeoutExpired so the test completes in milliseconds, not seconds.
        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        # Bypass task validation — the pre-flight is normally what enforces
        # that task_id exists in the DB; we set _skip_task_check via the
        # documented env toggle if available, otherwise run the full pipeline.
        # This test hits the DB via load_task_domain only when
        # domain_test_commands is set — our config doesn't set it, so the
        # DB round-trip is skipped.
        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()

        assert exit_code == 5, (
            f"expected exit 5 (timeout), got {exit_code}. "
            f"stdout={captured.out!r} stderr={captured.err!r}"
        )
        combined = captured.out + captured.err
        assert "timed out after 1s" in combined
        assert "test_command_timeout_sec" in combined

    def test_timeout_message_cites_env_source_when_env_set(
        self, tmp_path, monkeypatch, capsys
    ):
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "sleep 30"})
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "1")

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert exit_code == 5
        assert "TUSK_TEST_COMMAND_TIMEOUT" in combined

    def test_timeout_with_bytes_payload_does_not_crash_issue561(
        self, tmp_path, monkeypatch, capsys
    ):
        # Regression: subprocess.TimeoutExpired can carry raw bytes on
        # output/stderr even when subprocess.run was called with text=True
        # (the buffered payload is captured before decoding when the timeout
        # fires).  The dump path at tusk-commit.py:744 used to call
        # sys.stdout.write(exc.stdout) unconditionally, which crashes with
        # `TypeError: write() argument must be str, not bytes` on the bytes
        # branch.  This test pins the defensive decode in place.
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=1,
                    output=b"buffered stdout from hung test\n",
                    stderr=b"buffered stderr from hung test\n",
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert exit_code == 5, (
            f"expected exit 5 (timeout), got {exit_code}. "
            f"stdout={captured.out!r} stderr={captured.err!r}"
        )
        assert "TypeError" not in combined
        assert "buffered stdout from hung test" in captured.out
        assert "buffered stderr from hung test" in captured.err
        assert "timed out after 1s" in combined

    def test_timeout_with_str_payload_still_writes_unchanged(
        self, tmp_path, monkeypatch, capsys
    ):
        # Sibling case: when the buffered payload is already str (the common
        # path), the new defensive branch must pass it through verbatim.
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=1,
                    output="str stdout payload\n",
                    stderr="str stderr payload\n",
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()

        assert exit_code == 5
        assert "str stdout payload" in captured.out
        assert "str stderr payload" in captured.err


# ── Auto-scale (TASK-192 / Issue #575) ──────────────────────────────────────


def _make_db_with_test_runs(tmp_path) -> str:
    """Create a temp DB containing only the test_runs table.

    Mirrors the migration-62 schema verbatim so tests don't depend on the
    full bin/tusk init pipeline. Returns the absolute path.
    """
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                session_id INTEGER,
                test_command TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL,
                succeeded INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX idx_test_runs_command_succeeded_id
                ON test_runs(test_command, succeeded, id DESC);
        """)
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


def _seed_test_runs(db_path: str, test_command: str, elapsed_seconds: list[float],
                    succeeded: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO test_runs (test_command, elapsed_seconds, succeeded) "
            "VALUES (?, ?, ?)",
            [(test_command, e, 1 if succeeded else 0) for e in elapsed_seconds],
        )
        conn.commit()
    finally:
        conn.close()


class TestAutoScale:
    """Auto-scale layer: p95(last_N_successes) * multiplier, floored at default."""

    def test_cold_start_fewer_than_n_samples_returns_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = _make_db_with_test_runs(tmp_path)
        # Seed 5 runs; threshold is AUTO_TIMEOUT_SAMPLE_COUNT (20).
        _seed_test_runs(db_path, "pytest", [5.0] * 5)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"

    def test_cold_start_exactly_one_below_threshold_returns_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = _make_db_with_test_runs(tmp_path)
        _seed_test_runs(db_path, "pytest",
                        [5.0] * (mod.AUTO_TIMEOUT_SAMPLE_COUNT - 1))
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        assert source == "default"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_warm_state_returns_auto_when_p95_times_multiplier_exceeds_default(
            self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = _make_db_with_test_runs(tmp_path)
        # 20 samples in [100..200]s — p95 ≈ 200s, *2 = 400s, exceeds the 240 default.
        samples = [100.0 + 5.0 * i for i in range(mod.AUTO_TIMEOUT_SAMPLE_COUNT)]
        _seed_test_runs(db_path, "pytest", samples)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        # p95 of 20 sorted samples = sorted[ceil(0.95*20)-1] = sorted[18] = 190.0
        # ceil(190.0 * 2.0) = 380
        assert source == "auto"
        assert timeout == 380

    def test_warm_state_floors_at_default_when_p95_low(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = _make_db_with_test_runs(tmp_path)
        # Fast suite: 20 samples around 30s. ceil(30 * 2) = 60s, well under 240.
        _seed_test_runs(db_path, "pytest", [30.0] * mod.AUTO_TIMEOUT_SAMPLE_COUNT)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        # Auto layer DID fire — but the floor wins. The source label is still
        # "auto" because the auto layer is what produced the value (just
        # clamped to the floor); rerunning auto-scale would give the same
        # answer until samples grow.
        assert source == "auto"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_env_overrides_auto(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "77")
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = _make_db_with_test_runs(tmp_path)
        # Seed enough samples that auto WOULD fire if env weren't set.
        _seed_test_runs(db_path, "pytest", [200.0] * mod.AUTO_TIMEOUT_SAMPLE_COUNT)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        assert timeout == 77
        assert source == "env"

    def test_config_overrides_auto(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {
            "test_command": "pytest", "test_command_timeout_sec": 99,
        })
        db_path = _make_db_with_test_runs(tmp_path)
        _seed_test_runs(db_path, "pytest", [200.0] * mod.AUTO_TIMEOUT_SAMPLE_COUNT)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        assert timeout == 99
        assert source == "config"

    def test_test_command_scoping_isolates_histories(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest -n auto"})
        db_path = _make_db_with_test_runs(tmp_path)
        # Bulk samples for plain `pytest`; the resolver query is for a different
        # command and must NOT see them.
        _seed_test_runs(db_path, "pytest", [200.0] * mod.AUTO_TIMEOUT_SAMPLE_COUNT)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest -n auto")
        assert source == "default"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_failed_runs_excluded_from_p95(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = _make_db_with_test_runs(tmp_path)
        # 5 successful + 20 failed. Auto query filters succeeded=1, so only 5
        # successes are visible — under threshold → fall through to default.
        _seed_test_runs(db_path, "pytest", [10.0] * 5, succeeded=True)
        _seed_test_runs(db_path, "pytest", [500.0] * 20, succeeded=False)
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        assert source == "default"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_omitting_db_path_disables_auto(self, tmp_path, monkeypatch):
        """Backward-compat: existing callers that pass only config_path
        behave identically to the pre-auto resolver."""
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert source == "default"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_missing_db_file_falls_through_to_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        timeout, source = mod.load_test_command_timeout(
            cfg, str(tmp_path / "does-not-exist.db"), "pytest")
        assert source == "default"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_missing_test_runs_table_falls_through(self, tmp_path, monkeypatch):
        """Pre-migration installs (DB exists, table doesn't) must not break
        the commit path."""
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        db_path = str(tmp_path / "tasks.db")
        # Empty DB — no test_runs table.
        sqlite3.connect(db_path).close()
        timeout, source = mod.load_test_command_timeout(cfg, db_path, "pytest")
        assert source == "default"
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_compute_auto_timeout_respects_sample_count_window(self, tmp_path):
        """Verify the resolver uses the most recent N samples, not all rows."""
        mod = _load_module()
        db_path = _make_db_with_test_runs(tmp_path)
        # Old slow runs followed by a window of fast runs equal to the threshold.
        _seed_test_runs(db_path, "pytest", [500.0] * 10)
        _seed_test_runs(db_path, "pytest", [40.0] * mod.AUTO_TIMEOUT_SAMPLE_COUNT)
        result = mod._compute_auto_timeout(db_path, "pytest")
        # Fast window: p95 = 40, *2 = 80, floored at 240.
        assert result == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC

    def test_record_test_run_inserts_row(self, tmp_path):
        mod = _load_module()
        db_path = _make_db_with_test_runs(tmp_path)
        mod._record_test_run(db_path, task_id=42, test_command="pytest",
                             elapsed_seconds=12.5, succeeded=True)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT task_id, test_command, elapsed_seconds, succeeded "
                "FROM test_runs"
            ).fetchone()
        finally:
            conn.close()
        assert row == (42, "pytest", 12.5, 1)

    def test_record_test_run_silent_on_missing_db(self, tmp_path):
        """Persistence is best-effort — must never raise."""
        mod = _load_module()
        # No exception should propagate.
        mod._record_test_run(str(tmp_path / "missing.db"), task_id=1,
                             test_command="pytest", elapsed_seconds=1.0)

    def test_record_test_run_silent_on_missing_table(self, tmp_path):
        """Pre-migration install with a DB but no test_runs table — silent skip."""
        mod = _load_module()
        db_path = str(tmp_path / "empty.db")
        sqlite3.connect(db_path).close()
        # No exception should propagate.
        mod._record_test_run(db_path, task_id=1, test_command="pytest",
                             elapsed_seconds=1.0)

    def test_record_test_run_skips_empty_command(self, tmp_path):
        mod = _load_module()
        db_path = _make_db_with_test_runs(tmp_path)
        mod._record_test_run(db_path, task_id=1, test_command="",
                             elapsed_seconds=1.0)
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
        finally:
            conn.close()
        assert count == 0
