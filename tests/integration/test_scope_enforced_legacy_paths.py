"""Regression coverage for TASK-472: when ``tasks.scope_enforced=0`` (legacy
backfilled tasks from migration 73), all three retroactive scope-detection
heuristics MUST still fire. The scope_enforced=1 bypass is opt-in and must
not retroactively change behavior for unenforced tasks.

Three surfaces are exercised, one test per heuristic:
  - tusk task-unstart   — file-overlap prefix-collision heuristic (issue #627)
  - tusk task-summary   — block-level scope filter (issues #663, #670)
  - tusk check-deliverables — ``merged_not_closed_low_confidence`` downgrade
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


tusk_task_unstart = _load("tusk-task-unstart")
tusk_task_summary = _load("tusk-task-summary")
tusk_check_deliverables = _load("tusk-check-deliverables")


# ── nested repo setup (shared with the per-surface test files) ───────


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


def _insert_task(db_file, summary, description, status, scope_enforced):
    started = "datetime('now')" if status != "To Do" else "NULL"
    extra_cols = ""
    extra_vals = ""
    if status == "Done":
        extra_cols = ", closed_at, closed_reason"
        extra_vals = ", datetime('now'), 'completed'"
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            f"complexity, priority_score, started_at, scope_enforced{extra_cols}) "
            "VALUES (?, ?, ?, 'feature', 'Medium', 'S', 50, "
            f"{started}, ?{extra_vals})",
            (summary, description, status, scope_enforced),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _call_unstart(db_file, config_path, task_id):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_unstart.main(
            [str(db_file), str(config_path), str(task_id), "--force"]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


def _call_summary(db_file, config_path, task_id):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_task_summary.main(
            [str(db_file), str(config_path), str(task_id), "--format", "json"]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


def _call_check_deliverables(db_file, config_path, task_id):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = tusk_check_deliverables.main(
            [str(db_file), str(config_path), str(task_id)]
        )
    out = out_buf.getvalue().strip()
    result = json.loads(out) if out and out.startswith("{") else None
    return rc, result, err_buf.getvalue()


# ── task-unstart prefix-collision heuristic still fires under scope_enforced=0 ─


def test_legacy_task_unstart_still_runs_prefix_collision_filter(
    tmp_path, config_path, monkeypatch
):
    """Issue #627 path: with scope_enforced=0, an off-scope [TASK-N] commit
    is still treated as a prefix-match false positive and unstart succeeds.

    Same fixture shape as
    ``test_unstart_succeeds_when_historical_commit_unrelated_to_task_paths``
    in test_task_unstart.py — but pinned here under the legacy-paths file so
    the regression is unambiguous if migration 73's backfill semantics change.
    """
    db = _setup_nested_repo(tmp_path, monkeypatch)
    task_id = _insert_task(
        db,
        summary="Ship ios-libs-contribute skill",
        description="Lives at skills/ios-libs-contribute/SKILL.md and registers in CLAUDE.md.",
        status="In Progress",
        scope_enforced=0,
    )
    _git_commit_with_files(
        tmp_path,
        f"[TASK-{task_id}] Skip branch-naming check gracefully",
        [(".claude/hooks/branch-naming.sh", "#!/bin/bash\necho hi\n")],
    )

    rc, result, err = _call_unstart(db, config_path, task_id)
    assert rc == 0, (
        f"expected legacy heuristic to fire and unstart to succeed; "
        f"got rc={rc}, stderr={err}"
    )
    assert result is not None
    assert result["task"]["status"] == "To Do"
    # No bypass note should have been emitted.
    assert "bypassed prefix-collision check" not in err


# ── task-summary block-level filter still fires under scope_enforced=0 ─


def test_legacy_task_summary_still_runs_block_filter(
    tmp_path, config_path, monkeypatch
):
    """Issue #663 path: with scope_enforced=0, the block-level scope filter
    code path is taken — verified by the absence of the TASK-472 bypass
    stderr note (force-emitted under TUSK_FORCE_WARN if the bypass fires).

    A direct "filter drops the off-scope commit" assertion is not viable
    here because ``filter_commits_by_block_overlap`` has an extraction-miss
    fallthrough (issue #851): when zero blocks intersect the scope signal,
    it returns the commits unchanged to avoid silent zero-range refusal on
    off-scope precedent citations. So the legacy behavior on a one-commit,
    no-overlap fixture is "keep the commit" — same observable result as
    the scope_enforced=1 bypass, but produced by a different code path.
    The bypass-note assertion is what distinguishes them.
    """
    db = _setup_nested_repo(tmp_path, monkeypatch)
    task_id = _insert_task(
        db,
        summary="Document the foo skill",
        description="Update skills/foo/SKILL.md with the new instructions.",
        status="Done",
        scope_enforced=0,
    )
    _git_commit_with_files(
        tmp_path,
        f"[TASK-{task_id}] adjust CHANGELOG",
        [("CHANGELOG.md", "# log\n")],
    )

    monkeypatch.setenv("TUSK_FORCE_WARN", "1")
    rc, result, err = _call_summary(db, config_path, task_id)
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result is not None
    # The bypass code path must not have run — its stderr note is force-
    # emitted under TUSK_FORCE_WARN, so its absence proves the heuristic
    # was reached.
    assert "bypassed block-level scope filter" not in err, (
        f"unexpected bypass note for scope_enforced=0 task; stderr={err!r}"
    )


# ── check-deliverables low-confidence downgrade still fires under scope_enforced=0 ─


def test_legacy_check_deliverables_still_downgrades(
    tmp_path, config_path, monkeypatch
):
    """Issue #606 path: with scope_enforced=0, an off-scope [TASK-N] commit
    on the default branch still produces ``merged_not_closed_low_confidence``.
    """
    db = _setup_nested_repo(tmp_path, monkeypatch)
    task_id = _insert_task(
        db,
        summary="Document the foo skill",
        description="Update skills/foo/SKILL.md with the new instructions.",
        status="In Progress",
        scope_enforced=0,
    )
    _git_commit_with_files(
        tmp_path,
        f"[TASK-{task_id}] adjust CHANGELOG",
        [("CHANGELOG.md", "# log\n")],
    )

    rc, result, err = _call_check_deliverables(db, config_path, task_id)
    assert rc == 0, f"expected 0, got {rc}; stderr={err}"
    assert result is not None
    assert result["recommendation"] == "merged_not_closed_low_confidence", (
        f"expected legacy downgrade under scope_enforced=0, "
        f"got {result['recommendation']}; result={result!r}"
    )
    assert "bypassed scope-overlap downgrade" not in err
