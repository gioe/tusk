"""Regression tests for tusk-review.py main() dispatch hardening (issue #1029).

The reporter saw `tusk review status <task_id>` "exit 1 with no diagnostic
output" — which is the literal bin/tusk silent-exit-guard message that fires
when a subcommand exits nonzero while writing nothing to stderr. The one
in-script path that produced that shape was the no-subcommand branch, which
printed help to *stdout* and exited 1 with an empty stderr. main() now:

  - prints an actionable error to stderr (plus help) and exits 2 when no
    subcommand is given, so a nonzero exit always carries a reason; and
  - has a defensive final else that emits a named diagnostic and exits nonzero
    on a parser/handler mismatch instead of silently returning 0.

These tests invoke the script directly (python3 bin/tusk-review.py <db>
<config> [args]) so they exercise main()'s real dispatch without the bin/tusk
wrapper's silent-exit guard in the way.
"""

import os
import sqlite3
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-review.py")


def _run(db_path: str, config_path: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT, db_path, config_path, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_db(tmp_path, *, with_approved_review=False) -> str:
    """Build a minimal SQLite DB with the columns cmd_status reads."""
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT);
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            reviewer TEXT,
            status TEXT,
            review_pass INTEGER,
            created_at TEXT DEFAULT '2026-01-01',
            updated_at TEXT DEFAULT '2026-01-01'
        );
        CREATE TABLE review_comments (
            id INTEGER PRIMARY KEY,
            review_id INTEGER,
            resolution TEXT
        );
        """
    )
    if with_approved_review:
        conn.execute("INSERT INTO tasks (id, summary) VALUES (2677, 'closeout task')")
        conn.execute(
            "INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)"
            " VALUES (5006, 2677, NULL, 'approved', 1)"
        )
    conn.commit()
    conn.close()
    return db_path


def test_no_subcommand_writes_actionable_stderr_and_exits_nonzero(tmp_path):
    # The reported failure shape: a nonzero exit that carried no diagnostic on
    # stderr. The fix must put an actionable message on stderr (not only help
    # on stdout) and exit nonzero. db/config can be dummies — parsing fails
    # before any DB access.
    res = _run(str(tmp_path / "nonexistent.db"), str(tmp_path / "config.json"))
    assert res.returncode != 0
    assert res.returncode == 2
    # The actionable diagnostic is on stderr, not buried in stdout-only help.
    assert "no subcommand given" in res.stderr
    assert "tusk review --help" in res.stderr


def test_status_for_approved_review_exits_zero_with_json(tmp_path):
    # Regression guard for the path the reporter expected to work: an approved,
    # comment-free review resolves to exit 0 with the approved status in JSON.
    db_path = _make_db(tmp_path, with_approved_review=True)
    res = _run(db_path, str(tmp_path / "config.json"), "status", "2677")
    assert res.returncode == 0, res.stderr
    import json

    payload = json.loads(res.stdout)
    assert payload["task_id"] == 2677
    assert len(payload["reviews"]) == 1
    assert payload["reviews"][0]["status"] == "approved"
    assert payload["reviews"][0]["comment_counts"]["total"] == 0


def test_unknown_subcommand_exits_nonzero_with_usage(tmp_path):
    # argparse rejects an invalid choice before dispatch — exit 2 with a usage
    # message naming the bad command. This confirms the guarantee "never exit
    # nonzero with empty stderr" holds for the bad-choice path too.
    res = _run(str(tmp_path / "nonexistent.db"), str(tmp_path / "config.json"), "bogus")
    assert res.returncode == 2
    assert res.stderr.strip() != ""
    assert "bogus" in res.stderr
