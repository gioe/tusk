"""Unit coverage for review finding spec-gap classification."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-review.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_review", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "review_categories": ["must_fix", "suggest"],
                "review_severities": ["critical", "major", "minor"],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _db(tmp_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT
        );
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            reviewer TEXT,
            status TEXT,
            review_pass INTEGER,
            diff_summary TEXT,
            note TEXT,
            created_at TEXT DEFAULT '2026-01-01',
            updated_at TEXT DEFAULT '2026-01-01'
        );
        CREATE TABLE review_comments (
            id INTEGER PRIMARY KEY,
            review_id INTEGER,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            category TEXT,
            severity TEXT,
            comment TEXT,
            resolution TEXT,
            resolution_note TEXT,
            spec_gap_type TEXT,
            created_at TEXT DEFAULT '2026-01-01',
            updated_at TEXT DEFAULT '2026-01-01'
        );
        INSERT INTO tasks (id, summary) VALUES (1, 'sample task');
        INSERT INTO code_reviews (id, task_id, reviewer, status, review_pass)
        VALUES (1, 1, 'codex', 'pending', 1);
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


def test_add_comment_records_spec_gap_type(tmp_path):
    review = _load_module()
    db_path = _db(tmp_path)
    args = argparse.Namespace(
        review_id=1,
        comment="No criterion covers the failure mode",
        file="bin/tusk-review.py",
        line_start=10,
        line_end=None,
        category="must_fix",
        severity="major",
        spec_gap_type="missing_criterion",
    )

    assert review.cmd_add_comment(args, db_path, _write_config(tmp_path)) == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT spec_gap_type FROM review_comments WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["spec_gap_type"] == "missing_criterion"


def test_resolve_can_record_missing_verification_spec_gap_type(tmp_path):
    review = _load_module()
    db_path = _db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO review_comments (id, review_id, category, severity, comment) "
        "VALUES (1, 1, 'suggest', 'minor', 'Reviewer asked for proof')"
    )
    conn.commit()
    conn.close()
    args = argparse.Namespace(
        comment_id=1,
        resolution="dismissed",
        note="Tracked as follow-up",
        spec_gap_type="missing_verification",
    )

    assert review.cmd_resolve(args, db_path) == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT resolution, resolution_note, spec_gap_type FROM review_comments WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["resolution"] == "dismissed"
    assert row["resolution_note"] == "Tracked as follow-up"
    assert row["spec_gap_type"] == "missing_verification"


def test_list_json_exposes_spec_gap_type(tmp_path, capsys):
    review = _load_module()
    db_path = _db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO review_comments "
        "(id, review_id, category, severity, comment, spec_gap_type) "
        "VALUES (1, 1, 'must_fix', 'major', 'Ambiguous requirement', 'ambiguous_spec')"
    )
    conn.commit()
    conn.close()

    assert review.cmd_list(argparse.Namespace(task_id=1), db_path) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["comments"][0]["spec_gap_type"] == "ambiguous_spec"


def test_summary_exposes_spec_gap_patterns(tmp_path, capsys):
    review = _load_module()
    db_path = _db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO review_comments "
        "(id, review_id, category, severity, comment, spec_gap_type) "
        "VALUES (1, 1, 'must_fix', 'major', 'Missing proof', 'missing_verification')"
    )
    conn.execute(
        "INSERT INTO review_comments "
        "(id, review_id, category, severity, comment, spec_gap_type) "
        "VALUES (2, 1, 'suggest', 'minor', 'Spec surprised us', 'design_discovery')"
    )
    conn.commit()
    conn.close()

    assert review.cmd_summary(argparse.Namespace(task_id=1), db_path) == 0

    out = capsys.readouterr().out
    assert "Spec gaps:" in out
    assert "missing_verification=1" in out
    assert "design_discovery=1" in out
