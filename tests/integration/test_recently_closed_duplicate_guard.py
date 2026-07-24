"""End-to-end coverage for actionable recently closed duplicate matches."""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(env, *args):
    return subprocess.run(
        [TUSK_BIN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _insert(env, summary, *extra):
    return _run(
        env,
        "task-insert",
        summary,
        "Duplicate-guard integration fixture",
        "--criteria",
        "Fixture criterion",
        *extra,
    )


def _close_task(db_path, task_id, modifier):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status = 'Done', closed_reason = 'completed', "
            "closed_at = datetime('now', ?), updated_at = datetime('now', ?) "
            "WHERE id = ?",
            (modifier, modifier, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _task_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


def _write_plan(path, summary, duplicate_policy=None):
    task = {
        "summary": summary,
        "description": "Duplicate-guard import fixture",
        "criteria": ["Fixture criterion"],
    }
    if duplicate_policy is not None:
        task["duplicate_policy"] = duplicate_policy
    path.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")


def test_task_insert_blocks_recent_match_but_preserves_override_and_window(db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    recent_summary = "Recently completed direct-insert work"

    seeded = _insert(env, recent_summary)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    seeded_id = json.loads(seeded.stdout)["task_id"]
    _close_task(db_path, seeded_id, "0 days")

    blocked = _insert(env, recent_summary)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    blocked_payload = json.loads(blocked.stdout)
    assert blocked_payload == {
        "duplicate": True,
        "matched_task_id": seeded_id,
        "matched_summary": recent_summary,
        "similarity": 1.0,
    }

    overridden = _insert(env, recent_summary, "--skip-dupe")
    assert overridden.returncode == 0, overridden.stdout + overridden.stderr
    assert json.loads(overridden.stdout)["task_id"] != seeded_id

    old_summary = "Old completed direct-insert work"
    old_seed = _insert(env, old_summary)
    assert old_seed.returncode == 0, old_seed.stdout + old_seed.stderr
    old_id = json.loads(old_seed.stdout)["task_id"]
    _close_task(db_path, old_id, "-8 days")

    outside_window = _insert(env, old_summary)
    assert outside_window.returncode == 0, outside_window.stdout + outside_window.stderr
    assert json.loads(outside_window.stdout)["task_id"] != old_id


def test_task_import_applies_policies_to_recently_closed_matches(db_path, tmp_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    summary = "Recently completed imported work"

    seeded = _insert(env, summary)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    seeded_id = json.loads(seeded.stdout)["task_id"]
    _close_task(db_path, seeded_id, "0 days")
    baseline_count = _task_count(db_path)

    default_plan = tmp_path / "default.json"
    _write_plan(default_plan, summary)
    for extra in (("--dry-run",), ()):
        failed = _run(env, "task-import", "--file", str(default_plan), *extra)
        assert failed.returncode == 2, failed.stdout + failed.stderr
        failed_payload = json.loads(failed.stdout)
        assert failed_payload["failed"]["0"]["errors"] == [
            {
                "field": "duplicate_policy",
                "message": f"duplicate of TASK-{seeded_id}",
            }
        ]
        assert _task_count(db_path) == baseline_count

    skip_plan = tmp_path / "skip.json"
    _write_plan(skip_plan, summary, duplicate_policy="skip")
    for extra in (("--dry-run",), ()):
        skipped = _run(env, "task-import", "--file", str(skip_plan), *extra)
        assert skipped.returncode == 0, skipped.stdout + skipped.stderr
        skipped_payload = json.loads(skipped.stdout)
        assert skipped_payload["skipped"]["0"] == {
            "reason": "duplicate",
            "matched_task_id": seeded_id,
            "matched_summary": summary,
            "similarity": 1.0,
        }
        assert _task_count(db_path) == baseline_count

    allow_plan = tmp_path / "allow.json"
    _write_plan(allow_plan, summary, duplicate_policy="allow")
    allowed = _run(env, "task-import", "--file", str(allow_plan))
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "task_id" in json.loads(allowed.stdout)["created"]["0"]
    assert _task_count(db_path) == baseline_count + 1
