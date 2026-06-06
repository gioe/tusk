import json
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _init_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tusk" / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return db_path


def _run(tmp_path, db_path, *args):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return subprocess.run(
        [TUSK_BIN, "context", *[str(arg) for arg in args]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _insert_task(tmp_path, db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    result = subprocess.run(
        [
            TUSK_BIN,
            "task-insert",
            "Context CLI integration",
            "Verify dispatcher wiring for context atoms.",
            "--criteria",
            "Context item can be added",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["task_id"]


def test_context_cli_round_trips_through_tusk_dispatcher(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    task_id = _insert_task(tmp_path, db_path)

    added = _run(
        tmp_path,
        db_path,
        "add",
        f"TASK-{task_id}",
        "--type",
        "entry_point",
        "--content",
        "Start with bin/tusk-context.py.",
        "--source",
        "manual",
    )
    assert added.returncode == 0, added.stderr
    item = json.loads(added.stdout)
    assert item["task_id"] == task_id
    assert item["item_type"] == "entry_point"

    listed = _run(tmp_path, db_path, "list", task_id)
    assert listed.returncode == 0, listed.stderr
    rows = json.loads(listed.stdout)
    assert [r["content"] for r in rows] == ["Start with bin/tusk-context.py."]

    resolved = _run(tmp_path, db_path, "resolve", item["id"])
    assert resolved.returncode == 0, resolved.stderr
    assert json.loads(resolved.stdout)["status"] == "resolved"

    listed_active = _run(tmp_path, db_path, "list", task_id)
    assert listed_active.returncode == 0, listed_active.stderr
    assert json.loads(listed_active.stdout) == []

    listed_all = _run(tmp_path, db_path, "list", task_id, "--status", "all")
    assert listed_all.returncode == 0, listed_all.stderr
    assert json.loads(listed_all.stdout)[0]["status"] == "resolved"
