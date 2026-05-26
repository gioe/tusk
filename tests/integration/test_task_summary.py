"""Integration tests for tusk-task-summary.py focused on the scope_enforced
bypass introduced by TASK-472.

The block-level prefix-collision filter (issue #663) drops [TASK-N] commits
whose diff doesn't intersect the task's referenced paths. When the task is
``scope_enforced=1`` (default since migration 73), the commit-time scope
guard already filtered out-of-scope writes — running the block filter
afterwards is redundant and can incorrectly drop legitimate commits whose
diff lives entirely in paths the task description didn't pre-name.
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(REPO_ROOT, "bin", f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_task_summary = _load("tusk-task-summary")


def _setup_nested_repo(tmp_path, monkeypatch):
    """Initialize a tusk DB at tmp_path/tusk/tasks.db AND a git repo at tmp_path.

    Mirrors the layout used by test_task_unstart.py — the DB lives at
    ``tmp_path/tusk/tasks.db`` so the script's ``repo_root = dirname(dirname(db))``
    resolves to ``tmp_path``, where the git history is built.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(exist_ok=True)
    db_file = tusk_dir / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"tusk init failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)],
        check=True, capture_output=True, encoding="utf-8",
    )
    for k, v in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", k, v],
            check=True, capture_output=True, encoding="utf-8",
        )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "initial"],
        check=True, capture_output=True, encoding="utf-8",
    )
    return db_file


def _git_commit_with_files(repo_root, message, file_specs):
    for relpath, contents in file_specs:
        abs_path = os.path.join(str(repo_root), relpath)
        os.makedirs(os.path.dirname(abs_path) or str(repo_root), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(contents)
        subprocess.run(
            ["git", "-C", str(repo_root), "add", relpath],
            check=True, capture_output=True, encoding="utf-8",
        )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", message],
        check=True, capture_output=True, encoding="utf-8",
    )
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout.strip()


def _insert_done_task(db_file, summary, description, scope_enforced):
    """Insert a Done task. Leaves merge_commit_sha NULL so fetch_diff exercises
    the scan-and-filter path rather than the stamped-SHA fast path."""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at, closed_at, closed_reason, scope_enforced) "
            "VALUES (?, ?, 'Done', 'feature', 'Medium', 'S', 50, "
            "datetime('now', '-1 hour'), datetime('now'), 'completed', ?)",
            (summary, description, scope_enforced),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _call(db_path, config_path, *args):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_summary.main(
            [str(db_path), str(config_path), *[str(a) for a in args]]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


def test_scope_enforced_skips_block_filter(tmp_path, config_path, monkeypatch):
    """TASK-472: when scope_enforced=1, the block-level prefix-collision
    filter is skipped — every [TASK-N] commit shows up in diff stats even
    if its diff doesn't intersect the task's referenced paths.

    Fixture: task names ``skills/foo/SKILL.md`` but the only commit touches
    ``CHANGELOG.md`` (off-scope). With scope_enforced=0 the legacy block
    filter would drop it (commits=0); with scope_enforced=1 it's kept
    (commits=1).
    """
    db = _setup_nested_repo(tmp_path, monkeypatch)
    task_id = _insert_done_task(
        db,
        summary="Document the foo skill",
        description="Update skills/foo/SKILL.md with the new instructions.",
        scope_enforced=1,
    )
    # Off-scope commit — touches a file the task description never names.
    _git_commit_with_files(
        tmp_path,
        f"[TASK-{task_id}] adjust CHANGELOG",
        [("CHANGELOG.md", "# log\n")],
    )

    monkeypatch.setenv("TUSK_FORCE_WARN", "1")
    rc, result, err = _call(db, config_path, task_id, "--format", "json")
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result is not None
    # The off-scope [TASK-N] commit is preserved because scope_enforced=1
    # short-circuits the block filter.
    assert result["diff"]["commits"] == 1, (
        f"expected the off-scope commit to be kept under scope_enforced=1; "
        f"diff was {result['diff']!r}"
    )
    assert result["diff"]["files_changed"] == 1
    # Bypass stderr note fires under TUSK_FORCE_WARN.
    assert f"task-summary bypassed block-level scope filter for TASK-{task_id}" in err
