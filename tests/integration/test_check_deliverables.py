"""Integration tests for tusk-check-deliverables.py focused on the
scope_enforced bypass introduced by TASK-472.

Legacy tasks (scope_enforced=0) downgrade to ``merged_not_closed_low_confidence``
when [TASK-N] commits land on the default branch but their diff doesn't
intersect any path the task names. Under scope_enforced=1 the commit-time
guard already filtered out-of-scope writes, so the downgrade is unnecessary
and the recommendation stays as ``merged_not_closed``.
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


tusk_check_deliverables = _load("tusk-check-deliverables")


def _setup_nested_repo(tmp_path, monkeypatch):
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


def _insert_task(db_file, summary, description, scope_enforced):
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score, started_at, scope_enforced) "
            "VALUES (?, ?, 'In Progress', 'feature', 'Medium', 'S', 50, "
            "datetime('now'), ?)",
            (summary, description, scope_enforced),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _call(db_path, config_path, task_id):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_check_deliverables.main([str(db_path), str(config_path), str(task_id)])
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


def test_scope_enforced_trusts_merged(tmp_path, config_path, monkeypatch):
    """TASK-472: when scope_enforced=1, on-default [TASK-N] commits whose
    diff lives entirely outside the task's referenced paths still return
    ``merged_not_closed`` — the downgrade to ``merged_not_closed_low_confidence``
    is skipped because the commit-time guard guarantees the commits are
    legitimate.

    Same off-scope shape as the legacy low-confidence fixture: task names
    ``skills/foo/SKILL.md``; the commit on main touches ``CHANGELOG.md``.
    """
    db = _setup_nested_repo(tmp_path, monkeypatch)
    task_id = _insert_task(
        db,
        summary="Document the foo skill",
        description="Update skills/foo/SKILL.md with the new instructions.",
        scope_enforced=1,
    )
    _git_commit_with_files(
        tmp_path,
        f"[TASK-{task_id}] adjust CHANGELOG",
        [("CHANGELOG.md", "# log\n")],
    )

    monkeypatch.setenv("TUSK_FORCE_WARN", "1")
    rc, result, err = _call(db, config_path, task_id)
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result is not None
    assert result["recommendation"] == "merged_not_closed", (
        f"expected merged_not_closed under scope_enforced=1, "
        f"got {result['recommendation']}; result={result!r}"
    )
    assert result["commits_found"] is True
    assert len(result["default_branch_commits"]) == 1
    # Bypass stderr note fires under TUSK_FORCE_WARN.
    assert (
        f"check-deliverables bypassed scope-overlap downgrade for TASK-{task_id}"
        in err
    )
