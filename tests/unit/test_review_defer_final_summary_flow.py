"""Regression test for TASK-129: defer → review-defer → review-final-summary flow.

Before TASK-129, tusk-review-defer.py created a deferred task via
``tusk task-insert`` but then called ``tusk review resolve <id> deferred``
without threading the new task_id through — leaving
review_comments.deferred_task_id NULL. tusk-review-final-summary.py reads
deferred_task_id to distinguish "tasks created" from "skipped (duplicate)",
so the summary always reported 0 tasks created even when review-defer had
actually inserted them.

This test exercises the full pipeline end-to-end to pin the contract:
  1. Real tusk-review-defer.py logic drives the flow.
  2. The ``tusk task-insert`` and ``tusk dupes check`` subprocesses are
     stubbed (those are independent subsystems).
  3. The ``tusk review resolve`` subprocess is routed to the real
     ``cmd_resolve`` in tusk-review.py running against the same DB — so
     the actual UPDATE path is exercised, including the --deferred-task-id
     threading.
  4. tusk-review-final-summary.py runs as its own subprocess against the
     same DB and its output is asserted.

Assertion: the "N tasks created" number in the final-summary block matches
the count of comments that triggered a real task-insert (exit_code 0 from
dupes check), not the count of comments that skipped via duplicate match.
"""

import argparse
import importlib.util
import os
import sqlite3
import subprocess
import sys
import types


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(module_name: str, file_basename: str):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(BIN, file_basename)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod_defer = _load("tusk_review_defer", "tusk-review-defer.py")
mod_review = _load("tusk_review", "tusk-review.py")


_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT
);
CREATE TABLE code_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    reviewer TEXT,
    status TEXT DEFAULT 'pending',
    review_pass INTEGER DEFAULT 1,
    diff_summary TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    file_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    category TEXT,
    severity TEXT,
    comment TEXT NOT NULL,
    resolution TEXT DEFAULT NULL,
    deferred_task_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def _seed_review_with_defer_comments(tmp_path, comment_texts):
    """Create a DB with a task + one review + the given defer comments.

    Returns (db_path, review_id, [comment_ids]).
    """
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO tasks (id, summary) VALUES (7, 'Parent task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, status, review_pass)"
            " VALUES (1, 7, 'pending', 1)"
        )
        comment_ids = []
        for txt in comment_texts:
            cur = conn.execute(
                "INSERT INTO review_comments (review_id, category, severity, comment)"
                " VALUES (1, 'defer', 'minor', ?)",
                (txt,),
            )
            comment_ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return db_path, 1, comment_ids


def _make_stub_subprocess(db_path, insert_plan):
    """Build a subprocess.run stub for mod_defer.

    - ``tusk dupes check``: returns 0 (clean) or 1 (duplicate) per insert_plan.
    - ``tusk task-insert``: returns a fake task_id per insert_plan.
    - ``tusk review resolve``: routed to the real cmd_resolve against db_path
      so the actual UPDATE path (including --deferred-task-id) is exercised.

    insert_plan is a dict keyed by comment summary prefix:
      {"summary-prefix": {"dupe": 0|1, "task_id": N|None, "match_id": N|None}}
    """
    next_call = {"n": 0}

    def _find_plan(summary_arg: str) -> dict:
        for prefix, plan in insert_plan.items():
            if summary_arg.startswith(prefix):
                return plan
        raise AssertionError(f"No plan configured for summary: {summary_arg!r}")

    def _run(argv, capture_output=True, text=True, **kw):
        next_call["n"] += 1
        if len(argv) >= 2 and argv[1] == "dupes":
            summary_arg = argv[3]
            plan = _find_plan(summary_arg)
            stdout = '{"duplicates":[]}'
            if plan["dupe"] == 1 and plan.get("match_id") is not None:
                stdout = f'{{"duplicates":[{{"id":{plan["match_id"]},"similarity":0.9}}]}}'
            return subprocess.CompletedProcess(
                argv, plan["dupe"], stdout=stdout, stderr=""
            )
        if len(argv) >= 2 and argv[1] == "task-insert":
            summary_arg = argv[2]
            plan = _find_plan(summary_arg)
            task_id = plan["task_id"]
            return subprocess.CompletedProcess(
                argv, 0, stdout=f'{{"task_id":{task_id}}}', stderr=""
            )
        if len(argv) >= 3 and argv[1] == "review" and argv[2] == "resolve":
            ns = argparse.Namespace(
                comment_id=int(argv[3]),
                resolution=argv[4],
                deferred_task_id=None,
            )
            if "--deferred-task-id" in argv:
                ns.deferred_task_id = int(argv[argv.index("--deferred-task-id") + 1])
            rc = mod_review.cmd_resolve(ns, db_path)
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
        raise AssertionError(f"Unexpected subprocess call: {argv!r}")

    return _run


def _run_final_summary_cli(db_path, review_id):
    r = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-review-final-summary.py"),
         db_path, "fake.json", str(review_id)],
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout, r.stderr


class TestDeferFinalSummaryFlow:
    def test_created_count_matches_actual_inserts(self, tmp_path, monkeypatch):
        """Three defer comments: two trigger inserts, one is a duplicate.
        Final summary must report "2 tasks created, 1 skipped (duplicate)".

        Before TASK-129 this reported "0 tasks created, 3 skipped (duplicate)"
        because tusk-review-defer never linked the new task_id to the comment.
        """
        db_path, review_id, comment_ids = _seed_review_with_defer_comments(
            tmp_path,
            [
                "Race condition in foo()",
                "Memory leak in bar()",
                "Duplicate of existing task",
            ],
        )

        insert_plan = {
            "Race condition in foo()": {"dupe": 0, "task_id": 501, "match_id": None},
            "Memory leak in bar()": {"dupe": 0, "task_id": 502, "match_id": None},
            "Duplicate of existing task": {"dupe": 1, "task_id": None, "match_id": 88},
        }
        # Swap mod_defer's reference to the subprocess module for a shim
        # exposing only our stub run. Patching mod_defer.subprocess.run
        # directly would mutate the real subprocess module globally and
        # break the _run_final_summary_cli call below.
        shim = types.SimpleNamespace(run=_make_stub_subprocess(db_path, insert_plan))
        monkeypatch.setattr(mod_defer, "subprocess", shim)

        results = []
        for cid in comment_ids:
            results.append(
                mod_defer.defer_comment(db_path, cid, domain="cli", task_type="bug")
            )

        actual_creates = sum(1 for r in results if r["created_task_id"] is not None)
        actual_skips = sum(1 for r in results if r["skipped_reason"] == "duplicate")
        assert actual_creates == 2
        assert actual_skips == 1

        # Sanity: the DB now has deferred_task_id populated for the two created
        # comments and NULL for the duplicate — this is the linkage that was
        # missing before TASK-129.
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id, deferred_task_id FROM review_comments"
                " WHERE review_id = ? ORDER BY id",
                (review_id,),
            ).fetchall()
        finally:
            conn.close()
        populated = [r[1] for r in rows if r[1] is not None]
        assert sorted(populated) == [501, 502]

        rc, out, err = _run_final_summary_cli(db_path, review_id)
        assert rc == 0, err
        assert f"defer:     3 found, {actual_creates} tasks created, {actual_skips} skipped (duplicate)" in out

    def test_all_inserts_gives_zero_skips(self, tmp_path, monkeypatch):
        """Two defer comments, both clean → both create tasks → 0 skipped."""
        db_path, review_id, comment_ids = _seed_review_with_defer_comments(
            tmp_path,
            ["First finding", "Second finding"],
        )
        insert_plan = {
            "First finding": {"dupe": 0, "task_id": 901, "match_id": None},
            "Second finding": {"dupe": 0, "task_id": 902, "match_id": None},
        }
        # Swap mod_defer's reference to the subprocess module for a shim
        # exposing only our stub run. Patching mod_defer.subprocess.run
        # directly would mutate the real subprocess module globally and
        # break the _run_final_summary_cli call below.
        shim = types.SimpleNamespace(run=_make_stub_subprocess(db_path, insert_plan))
        monkeypatch.setattr(mod_defer, "subprocess", shim)

        for cid in comment_ids:
            mod_defer.defer_comment(db_path, cid, domain="cli", task_type="bug")

        rc, out, err = _run_final_summary_cli(db_path, review_id)
        assert rc == 0, err
        assert "defer:     2 found, 2 tasks created, 0 skipped (duplicate)" in out
