"""Integration coverage for task-insert's --skip-dupe/--force bypass (issue #1127).

Batch callers that already dedup on a stable identity key (e.g. one task per
venue keyed on google_place_id) need to insert tasks whose summaries fuzzy-match
a sibling filed seconds earlier — distinct venues sharing category nouns like
"Improv Theatre". Without a bypass, task-insert's internal guard returns
{"duplicate":true,...} and exits 1, silently dropping the venue.

End-to-end via subprocess against a real tusk DB:
  - default: a fuzzy duplicate still blocks (exit 1, duplicate JSON)
  - --skip-dupe: the same summary inserts (exit 0, new task_id)
  - --force: accepted alias, same bypass behavior
"""

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

# Two genuinely distinct venues whose summaries share category nouns; the issue
# reported these scoring 0.899 — comfortably above the 0.82 check_threshold.
SUMMARY_A = "Onboard scraper for Leela Improv Theatre (San Francisco, CA)"
SUMMARY_B = "Onboard scraper for BATS Improv Theatre (San Francisco, CA)"


def _run(cmd, env, check=False):
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", env=env
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {cmd}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}\n"
            f"exit={result.returncode}"
        )
    return result


def _insert(env, summary, *extra, check=False):
    return _run(
        [TUSK_BIN, "task-insert", summary, "Onboard the venue's show scraper",
         "--criteria", "Scraper produces a real scrape", *extra],
        env,
        check=check,
    )


def test_skip_dupe_bypasses_fuzzy_guard(db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)

    # First venue inserts cleanly.
    first = _insert(env, SUMMARY_A, check=True)
    first_id = json.loads(first.stdout)["task_id"]

    # Second venue WITHOUT the flag: the fuzzy guard fires and blocks.
    blocked = _insert(env, SUMMARY_B)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    blocked_json = json.loads(blocked.stdout)
    assert blocked_json["duplicate"] is True
    assert blocked_json["matched_task_id"] == first_id

    # Second venue WITH --skip-dupe: inserts past the would-be duplicate.
    forced = _insert(env, SUMMARY_B, "--skip-dupe")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    forced_json = json.loads(forced.stdout)
    assert "task_id" in forced_json
    assert forced_json["task_id"] != first_id
    assert "duplicate" not in forced_json


def test_force_alias_bypasses_fuzzy_guard(db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)

    first = _insert(env, SUMMARY_A, check=True)
    first_id = json.loads(first.stdout)["task_id"]

    # --force is the documented alias for --skip-dupe.
    forced = _insert(env, SUMMARY_B, "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    forced_json = json.loads(forced.stdout)
    assert forced_json["task_id"] != first_id
    assert "duplicate" not in forced_json
