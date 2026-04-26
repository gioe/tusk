"""Integration test for the `tusk deps show` alias of `tusk deps list` (Issue #579)."""

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(env, *args):
    return subprocess.run(
        [TUSK_BIN, *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _seed_task(env):
    result = _run(env, "task-insert", "alias regression target", "body", "--criteria", "verify alias")
    assert result.returncode == 0, result.stderr
    import json
    return json.loads(result.stdout)["task_id"]


def test_show_dispatches_identically_to_list(db_path):
    env = {**os.environ, "TUSK_DB": str(db_path)}
    task_id = _seed_task(env)

    list_result = _run(env, "deps", "list", str(task_id))
    show_result = _run(env, "deps", "show", str(task_id))

    assert list_result.returncode == 0, list_result.stderr
    assert show_result.returncode == 0, show_result.stderr
    assert show_result.stdout == list_result.stdout


def test_show_supports_json_flag_like_list(db_path):
    env = {**os.environ, "TUSK_DB": str(db_path)}
    task_id = _seed_task(env)

    list_result = _run(env, "deps", "list", str(task_id), "--json")
    show_result = _run(env, "deps", "show", str(task_id), "--json")

    assert list_result.returncode == 0
    assert show_result.returncode == 0
    assert show_result.stdout == list_result.stdout
