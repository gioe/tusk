"""Integration test for the Issue #603 repro scenario.

End-to-end via subprocess against a real tusk DB: an In-Progress parent task
with an open acceptance criterion is surfaced by `tusk dupes check` when the
proposed summary matches the criterion text.
"""

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(cmd, env, check=True):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {cmd}\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}\n"
            f"exit={result.returncode}"
        )
    return result


def test_in_progress_criterion_surfaces_as_duplicate(db_path):
    """Repro from Issue #603: open criterion on an In-Progress task should
    be surfaced by `tusk dupes check` when the proposed summary matches.

    Asserts:
      - `dupes check --json` exits 1 and reports the parent task ID
      - the match_type is 'criterion' and includes criterion_id + criterion text
    """
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)

    # Insert the parent task with an acceptance criterion. The criterion text
    # is exactly what would be matched by /create-task input that duplicated
    # an existing in-flight scope.
    insert = _run(
        [
            TUSK_BIN, "task-insert",
            "Test parent",
            "Parent task",
            "--priority", "Low",
            "--criteria",
            "Add iOS networking layer with URLSession and Bearer auth",
        ],
        env=env,
    )
    parent = json.loads(insert.stdout)
    parent_id = parent["task_id"]
    criterion_id = parent["criteria_ids"][0]

    # Move the parent into 'In Progress' so the new criterion-scan path picks
    # it up — completed/Done tasks are excluded by design.
    # task-start is the user-facing way to flip status to In Progress; it
    # opens a session as a side effect, which is fine for this scenario.
    _run([TUSK_BIN, "task-start", str(parent_id)], env=env)

    # The actual repro: dupe-check a summary identical to the criterion text.
    check = _run(
        [
            TUSK_BIN, "dupes", "check", "--json",
            "Add iOS networking layer with URLSession and Bearer auth",
        ],
        env=env,
        check=False,
    )

    assert check.returncode == 1, (
        f"Expected exit 1 (duplicates found), got {check.returncode}.\n"
        f"STDOUT: {check.stdout}\n"
        f"STDERR: {check.stderr}"
    )
    payload = json.loads(check.stdout)
    matches = payload["duplicates"]
    assert len(matches) == 1, f"Expected 1 match, got {len(matches)}: {matches}"

    m = matches[0]
    assert m["match_type"] == "criterion"
    assert m["id"] == parent_id
    assert m["criterion_id"] == criterion_id
    assert m["criterion"] == (
        "Add iOS networking layer with URLSession and Bearer auth"
    )


def test_in_progress_criterion_ignored_when_completed(db_path):
    """Once the criterion is marked done, the criterion-scan path should
    ignore it — completed work is not a duplicate of follow-up tasks."""
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)

    insert = _run(
        [
            TUSK_BIN, "task-insert",
            "Test parent",
            "Parent task",
            "--priority", "Low",
            "--criteria",
            "Add iOS networking layer with URLSession and Bearer auth",
        ],
        env=env,
    )
    parent = json.loads(insert.stdout)
    parent_id = parent["task_id"]
    criterion_id = parent["criteria_ids"][0]

    # task-start is the user-facing way to flip status to In Progress; it
    # opens a session as a side effect, which is fine for this scenario.
    _run([TUSK_BIN, "task-start", str(parent_id)], env=env)
    _run(
        [TUSK_BIN, "criteria", "done", str(criterion_id), "--skip-verify"],
        env=env,
    )

    check = _run(
        [
            TUSK_BIN, "dupes", "check", "--json",
            "Add iOS networking layer with URLSession and Bearer auth",
        ],
        env=env,
        check=False,
    )
    # Parent summary is "Test parent" — unrelated to the proposed input —
    # so neither the summary nor the (now-completed) criterion should match.
    assert check.returncode == 0
    payload = json.loads(check.stdout)
    assert payload["duplicates"] == []
