"""Regression: tusk criteria add/done/skip/reset emit compact JSON on stdout.

Issue #590: programmatic callers had to regex-parse plain text output. Every
state-changing tusk CLI command returns JSON, except this family did not.
This test asserts ``json.loads`` succeeds on each subcommand's stdout — both
the success path and the idempotent no-op paths.
"""

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(cmd, cwd, env=None, check=True):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _git_init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main", str(repo)], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "seed"], cwd=repo)


def _insert_task(repo, env):
    inserted = _run(
        [
            TUSK_BIN, "task-insert",
            "JSON output regression",
            "Fixture for criteria JSON output.",
            "--priority", "Medium",
            "--domain", "cli",
            "--task-type", "bug",
            "--complexity", "S",
            "--criteria", "seed criterion (so the task isn't rejected for missing criteria)",
        ],
        cwd=repo,
        env=env,
    )
    return json.loads(inserted.stdout)["task_id"]


def test_criteria_add_emits_json(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    task_id = _insert_task(repo, env)

    added = _run(
        [TUSK_BIN, "criteria", "add", str(task_id), "JSON-shaped criterion",
         "--type", "manual"],
        cwd=repo,
        env=env,
    )
    obj = json.loads(added.stdout)
    assert obj["task_id"] == task_id
    assert obj["criterion_type"] == "manual"
    assert isinstance(obj["id"], int) and obj["id"] > 0


def test_criteria_done_emits_json_success_and_already_completed(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    task_id = _insert_task(repo, env)
    cid = json.loads(_run(
        [TUSK_BIN, "criteria", "add", str(task_id), "for done", "--type", "manual"],
        cwd=repo, env=env,
    ).stdout)["id"]

    done = _run(
        [TUSK_BIN, "criteria", "done", str(cid)],
        cwd=repo, env=env,
    )
    obj = json.loads(done.stdout)
    assert obj["id"] == cid
    assert obj["task_id"] == task_id
    assert obj["is_completed"] is True
    assert "criterion" in obj

    again = _run(
        [TUSK_BIN, "criteria", "done", str(cid)],
        cwd=repo, env=env,
    )
    obj2 = json.loads(again.stdout)
    assert obj2["id"] == cid
    assert obj2.get("already_completed") is True


def test_criteria_skip_emits_json_success_already_completed_and_already_deferred(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    task_id = _insert_task(repo, env)
    cid = json.loads(_run(
        [TUSK_BIN, "criteria", "add", str(task_id), "for skip", "--type", "manual"],
        cwd=repo, env=env,
    ).stdout)["id"]

    skipped = _run(
        [TUSK_BIN, "criteria", "skip", str(cid), "--reason", "out of scope"],
        cwd=repo, env=env,
    )
    obj = json.loads(skipped.stdout)
    assert obj["id"] == cid
    assert obj["is_deferred"] is True
    assert obj["deferred_reason"] == "out of scope"

    again = _run(
        [TUSK_BIN, "criteria", "skip", str(cid), "--reason", "still out of scope"],
        cwd=repo, env=env,
    )
    obj2 = json.loads(again.stdout)
    assert obj2.get("already_deferred") is True
    assert obj2["deferred_reason"] == "out of scope"

    cid2 = json.loads(_run(
        [TUSK_BIN, "criteria", "add", str(task_id), "for skip-after-done", "--type", "manual"],
        cwd=repo, env=env,
    ).stdout)["id"]
    _run([TUSK_BIN, "criteria", "done", str(cid2)], cwd=repo, env=env)
    skip_after_done = _run(
        [TUSK_BIN, "criteria", "skip", str(cid2), "--reason", "too late"],
        cwd=repo, env=env,
    )
    obj3 = json.loads(skip_after_done.stdout)
    assert obj3["id"] == cid2
    assert obj3.get("already_completed") is True


def test_criteria_reset_emits_json_success_and_already_incomplete(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    task_id = _insert_task(repo, env)
    cid = json.loads(_run(
        [TUSK_BIN, "criteria", "add", str(task_id), "for reset", "--type", "manual"],
        cwd=repo, env=env,
    ).stdout)["id"]

    no_op = _run(
        [TUSK_BIN, "criteria", "reset", str(cid)],
        cwd=repo, env=env,
    )
    obj0 = json.loads(no_op.stdout)
    assert obj0.get("already_incomplete") is True

    _run([TUSK_BIN, "criteria", "done", str(cid)], cwd=repo, env=env)
    reset = _run(
        [TUSK_BIN, "criteria", "reset", str(cid)],
        cwd=repo, env=env,
    )
    obj = json.loads(reset.stdout)
    assert obj["id"] == cid
    assert obj["is_completed"] is False
    assert obj["is_deferred"] is False
