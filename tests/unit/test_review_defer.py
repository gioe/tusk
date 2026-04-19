"""Unit tests for tusk-review-defer.py.

Covers (per TASK-115 criterion 507):
- created branch     — dupe check exits 0; task-insert is called; comment resolved;
                       returns {created_task_id: N, skipped_reason: null, matched_task_id: null}
- duplicate branch   — dupe check exits 1 with a duplicates[0].id; task-insert NOT called;
                       comment resolved; returns {created_task_id: null,
                       skipped_reason: "duplicate", matched_task_id: N}
- check-failed branch — dupe check exits 2; task-insert NOT called; comment resolved;
                        returns {created_task_id: null, skipped_reason: "dupe_check_failed",
                        matched_task_id: null}

Each test stubs ``subprocess.run`` on the module so no real ``tusk`` commands run.
A minimal on-disk SQLite DB with a real ``review_comments`` row drives the
``read_comment`` SQL path so the summary-extraction logic is exercised end-to-end.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import types

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
SCRIPT = os.path.join(BIN, "tusk-review-defer.py")


_spec = importlib.util.spec_from_file_location("tusk_review_defer", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── fixtures ───────────────────────────────────────────────────────────


def _make_db(tmp_path, comment_text="Race condition in foo()\n\nFull body with details."):
    """Create a minimal DB with one review_comments row, return (db_path, comment_id)."""
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE review_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                file_path TEXT,
                line_start INTEGER,
                line_end INTEGER,
                category TEXT,
                severity TEXT,
                comment TEXT NOT NULL,
                resolution TEXT,
                deferred_task_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur = conn.execute(
            "INSERT INTO review_comments (review_id, category, severity, comment) "
            "VALUES (1, 'defer', 'major', ?)",
            (comment_text,),
        )
        comment_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return str(db_path), comment_id


def _fake_run_factory(plan: dict):
    """Build a subprocess.run stub that dispatches on the tusk subcommand.

    plan keys: 'dupes', 'task-insert', 'review-resolve'. Each value is a dict
    with 'returncode', 'stdout', 'stderr'. Records call argvs into
    plan['calls'] in order so tests can assert what ran.
    """
    plan.setdefault("calls", [])

    def _run(argv, capture_output=True, text=True, **kw):
        plan["calls"].append(list(argv))
        # argv[0] is the tusk wrapper path; the first *subcommand* token is argv[1]
        if len(argv) >= 2 and argv[1] == "dupes":
            cfg = plan["dupes"]
        elif len(argv) >= 2 and argv[1] == "task-insert":
            cfg = plan["task-insert"]
        elif len(argv) >= 3 and argv[1] == "review" and argv[2] == "resolve":
            cfg = plan["review-resolve"]
        else:
            raise AssertionError(f"Unexpected subprocess call: {argv!r}")
        return subprocess.CompletedProcess(
            args=argv,
            returncode=cfg["returncode"],
            stdout=cfg.get("stdout", ""),
            stderr=cfg.get("stderr", ""),
        )

    return _run


# ── created branch ─────────────────────────────────────────────────────


class TestCreatedBranch:
    def test_dupe_check_clean_inserts_task_and_resolves(self, tmp_path, monkeypatch):
        db_path, comment_id = _make_db(tmp_path)

        plan = {
            "dupes": {"returncode": 0, "stdout": '{"duplicates":[]}\n'},
            "task-insert": {"returncode": 0, "stdout": '{"task_id":777,"summary":"x"}\n'},
            "review-resolve": {"returncode": 0, "stdout": "ok\n"},
        }
        monkeypatch.setattr(mod.subprocess, "run", _fake_run_factory(plan))

        result = mod.defer_comment(db_path, comment_id, domain="cli", task_type="bug")

        assert result == {
            "created_task_id": 777,
            "skipped_reason": None,
            "matched_task_id": None,
        }
        # Three subprocess calls: dupes, task-insert, review resolve
        subcmds = [c[1] for c in plan["calls"]]
        assert subcmds == ["dupes", "task-insert", "review"]
        # Summary passed to dupes is the first non-empty line, not the full body
        dupes_call = plan["calls"][0]
        assert "Race condition in foo()" in dupes_call
        assert "--domain" in dupes_call and "cli" in dupes_call
        # task-insert received --deferred, --task-type, and a criterion derived from summary
        ti_call = plan["calls"][1]
        assert "--deferred" in ti_call
        assert "--task-type" in ti_call and "bug" in ti_call
        criteria_idx = ti_call.index("--criteria")
        assert ti_call[criteria_idx + 1].startswith("Address deferred finding:")


# ── duplicate branch ───────────────────────────────────────────────────


class TestDuplicateBranch:
    def test_dupe_check_match_skips_insert_and_records_match(self, tmp_path, monkeypatch):
        db_path, comment_id = _make_db(tmp_path)

        plan = {
            "dupes": {
                "returncode": 1,
                "stdout": json.dumps(
                    {"duplicates": [{"id": 42, "summary": "other", "similarity": 0.91}]}
                ) + "\n",
            },
            "review-resolve": {"returncode": 0, "stdout": "ok\n"},
            # task-insert must NOT be called; leave it out so the fake run raises
            # AssertionError if it ever dispatches there.
        }
        monkeypatch.setattr(mod.subprocess, "run", _fake_run_factory(plan))

        result = mod.defer_comment(db_path, comment_id, domain="cli", task_type="bug")

        assert result == {
            "created_task_id": None,
            "skipped_reason": "duplicate",
            "matched_task_id": 42,
        }
        subcmds = [c[1] for c in plan["calls"]]
        assert subcmds == ["dupes", "review"]  # no task-insert


# ── check-failed branch ────────────────────────────────────────────────


class TestCheckFailedBranch:
    def test_dupe_check_error_skips_insert_and_still_resolves(self, tmp_path, monkeypatch):
        db_path, comment_id = _make_db(tmp_path)

        plan = {
            "dupes": {"returncode": 2, "stdout": "", "stderr": "boom\n"},
            "review-resolve": {"returncode": 0, "stdout": "ok\n"},
        }
        monkeypatch.setattr(mod.subprocess, "run", _fake_run_factory(plan))

        result = mod.defer_comment(db_path, comment_id, domain="cli", task_type="bug")

        assert result == {
            "created_task_id": None,
            "skipped_reason": "dupe_check_failed",
            "matched_task_id": None,
        }
        subcmds = [c[1] for c in plan["calls"]]
        assert subcmds == ["dupes", "review"]  # no task-insert


# ── error surface ──────────────────────────────────────────────────────


class TestErrors:
    def test_missing_comment_raises(self, tmp_path, monkeypatch):
        db_path, _ = _make_db(tmp_path)
        monkeypatch.setattr(mod.subprocess, "run", _fake_run_factory({
            "dupes": {"returncode": 0, "stdout": '{"duplicates":[]}'},
            "task-insert": {"returncode": 0, "stdout": '{"task_id":1}'},
            "review-resolve": {"returncode": 0, "stdout": ""},
        }))
        with pytest.raises(SystemExit) as exc:
            mod.defer_comment(db_path, 99999, domain="cli", task_type="bug")
        assert "99999" in str(exc.value)

    def test_resolve_failure_is_surfaced(self, tmp_path, monkeypatch):
        db_path, comment_id = _make_db(tmp_path)
        plan = {
            "dupes": {"returncode": 0, "stdout": '{"duplicates":[]}'},
            "task-insert": {"returncode": 0, "stdout": '{"task_id":1}'},
            "review-resolve": {"returncode": 2, "stderr": "Invalid resolution\n"},
        }
        monkeypatch.setattr(mod.subprocess, "run", _fake_run_factory(plan))
        with pytest.raises(SystemExit) as exc:
            mod.defer_comment(db_path, comment_id, domain="cli", task_type="bug")
        assert "review resolve" in str(exc.value)


# ── CLI-layer shape ────────────────────────────────────────────────────


class TestCLIShape:
    def test_main_requires_domain_and_task_type(self, tmp_path, capsys):
        db_path, comment_id = _make_db(tmp_path)
        with pytest.raises(SystemExit):
            mod.main([db_path, "fake.json", str(comment_id)])
        # argparse writes the error to stderr
        captured = capsys.readouterr()
        assert "--domain" in captured.err or "required" in captured.err

    def test_main_prints_json_on_success(self, tmp_path, monkeypatch, capsys):
        db_path, comment_id = _make_db(tmp_path)
        plan = {
            "dupes": {"returncode": 0, "stdout": '{"duplicates":[]}'},
            "task-insert": {"returncode": 0, "stdout": '{"task_id":5}'},
            "review-resolve": {"returncode": 0, "stdout": "ok"},
        }
        monkeypatch.setattr(mod.subprocess, "run", _fake_run_factory(plan))
        rc = mod.main([db_path, "fake.json", str(comment_id), "--domain", "cli", "--task-type", "bug"])
        out = capsys.readouterr().out.strip()
        assert rc == 0
        payload = json.loads(out)
        assert set(payload.keys()) == {"created_task_id", "skipped_reason", "matched_task_id"}
        assert payload["created_task_id"] == 5

    def test_main_rejects_bad_comment_id(self, tmp_path, capsys):
        db_path, _ = _make_db(tmp_path)
        rc = mod.main([db_path, "fake.json", "not-a-number", "--domain", "cli", "--task-type", "bug"])
        assert rc == 1
        assert "Invalid comment_id" in capsys.readouterr().err
