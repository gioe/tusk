"""End-to-end integration test for the mutually-exclusive criteria flow (issue #618).

Scenario: a task has two criteria expressing an OR relationship — "apply rate
limiting" vs "document why exempt". Implementation chose the first; the second
does not apply.

The user-facing claim being tested:
  1. `tusk criteria done` closes the chosen-branch criterion.
  2. `tusk criteria skip --reason "..."` closes the not-applicable criterion
     WITHOUT stamping it with a commit hash and WITHOUT requiring `--skip-verify`.
  3. `tusk task-done --reason completed` then succeeds WITHOUT `--force` —
     deferred criteria are excluded from the open-criteria gate.
  4. `tusk task-summary --format json` reports the deferred criterion under
     `criteria.deferred_details` with its `deferred_reason` preserved.

Runs end-to-end via subprocess against a real tusk DB (per
tests/integration/conftest fixture conventions).
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


def test_mutually_exclusive_criteria_close_cleanly(db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)

    # 1. Insert task with two mutually-exclusive criteria.
    insert = _run(
        [
            TUSK_BIN, "task-insert",
            "Add rate limiting to /upload",
            "Either rate-limit the endpoint or document why it's exempt.",
            "--priority", "Medium",
            "--criteria", "Apply rate limiting (chosen branch)",
            "--criteria", "Document why exempt (skipped branch)",
        ],
        env=env,
    )
    payload = json.loads(insert.stdout)
    task_id = payload["task_id"]
    chosen_cid, skipped_cid = payload["criteria_ids"]

    # 2. Move to In Progress so task-done sees it as a real workflow exit.
    _run([TUSK_BIN, "task-start", str(task_id), "--force"], env=env)

    # 3. Mark the chosen branch done. Use --skip-verify because there's no
    #    real commit in this test env — the verification path is orthogonal
    #    to what we're asserting (the open-criteria gate behavior).
    _run(
        [TUSK_BIN, "criteria", "done", str(chosen_cid), "--skip-verify"],
        env=env,
    )

    # 4. Skip the not-applicable branch with a free-text reason.
    skip_result = _run(
        [
            TUSK_BIN, "criteria", "skip", str(skipped_cid),
            "--reason", "not applicable: chose rate-limiting branch",
        ],
        env=env,
    )
    assert "deferred" in skip_result.stdout

    # 5. task-done WITHOUT --force must succeed — the deferred criterion
    #    is excluded from the open-criteria gate.
    done = _run(
        [TUSK_BIN, "task-done", str(task_id), "--reason", "completed"],
        env=env,
        check=False,
    )
    assert done.returncode == 0, (
        f"task-done was rejected even though the skipped criterion is deferred.\n"
        f"STDOUT: {done.stdout}\nSTDERR: {done.stderr}"
    )

    # 6. task-summary must report the deferred criterion with its reason.
    summary = _run(
        [TUSK_BIN, "task-summary", str(task_id), "--format", "json"],
        env=env,
    )
    data = json.loads(summary.stdout)
    crit = data["criteria"]
    assert crit["total"] == 2
    assert crit["deferred"] == 1
    assert len(crit["deferred_details"]) == 1
    detail = crit["deferred_details"][0]
    assert detail["id"] == skipped_cid
    assert detail["deferred_reason"] == "not applicable: chose rate-limiting branch"
    assert detail["criterion"] == "Document why exempt (skipped branch)"
