"""Regression coverage for task-start --skill reusing an open skill run."""

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, *, env, cwd=REPO_ROOT):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_task_start_skill_reuses_open_skill_run(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TUSK_DB": str(tmp_path / "tasks.db"),
        "TUSK_STATE_DIR": str(tmp_path / "state"),
    }

    init = _run(["init", "--force"], env=env)
    assert init.returncode == 0, init.stderr

    inserted = _run(
        [
            "task-insert",
            "Exercise duplicate skill run reuse",
            "Task used by a task-start --skill regression test.",
            "--criteria",
            "dummy criterion",
        ],
        env=env,
    )
    assert inserted.returncode == 0, inserted.stderr
    task_id = json.loads(inserted.stdout)["task_id"]

    first = _run(["task-start", str(task_id), "--force", "--skill", "tusk"], env=env)
    assert first.returncode == 0, first.stderr
    first_skill_run = json.loads(first.stdout)["skill_run"]

    second = _run(
        [
            "task-start",
            str(task_id),
            "--force",
            "--force-session",
            "--skill",
            "tusk",
        ],
        env=env,
    )
    assert second.returncode == 0, second.stderr
    second_skill_run = json.loads(second.stdout)["skill_run"]

    assert second_skill_run["run_id"] == first_skill_run["run_id"]
    assert second_skill_run["reused"] is True

    count = _run(
        [
            "SELECT COUNT(*) FROM skill_runs WHERE task_id = "
            f"{task_id} AND skill_name = 'tusk' AND ended_at IS NULL"
        ],
        env=env,
    )
    assert count.returncode == 0, count.stderr
    assert count.stdout.strip() == "1"
