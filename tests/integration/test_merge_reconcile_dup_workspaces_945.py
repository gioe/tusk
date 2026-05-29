"""Regression test for issue #945 / TASK-527.

``tusk merge``'s no-checkout success path cleaned only the *latest*
recorded ``task_workspaces`` row (``_recorded_task_workspace`` does
``ORDER BY id DESC LIMIT 1``). A task that ran ``task-worktree create``
more than once — e.g. two slugs for the same task — stranded the
un-selected sibling row(s) after finalization, and merge exited 3 with
duplicate recorded workspaces still present.

``_reconcile_duplicate_task_workspaces`` reconciles those siblings:

  * a sibling whose worktree path is gone -> registry row forgotten
    (and a fully-merged orphan branch deleted);
  * a sibling worktree with no unmerged work -> worktree + branch
    removed and the row forgotten;
  * a sibling holding unmerged commits -> left intact, a targeted
    remediation command surfaced, and the function returns False.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO_ROOT, "bin", f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_merge = _load("tusk-merge")


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _branch_exists(repo, branch):
    return (
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).returncode
        == 0
    )


@pytest.fixture()
def repo_with_origin(tmp_path, monkeypatch):
    """A real git repo on ``main`` with an ``origin`` bare remote, plus an
    initialized tusk DB at ``<repo>/tusk/tasks.db``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    db_path = repo / "tusk" / "tasks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TUSK_DB", str(db_path))
    r = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "tusk"), "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(repo),
    )
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tasks (id, summary, description, status, priority, closed_reason) "
        "VALUES (527, 's', 'd', 'In Progress', 'Medium', NULL)"
    )
    conn.commit()
    conn.close()

    # _reconcile chdir's to repo_root; restore cwd afterwards.
    monkeypatch.chdir(repo)
    return {"repo": repo, "db_path": str(db_path)}


def _insert_workspace(db_path, task_id, branch, path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO task_workspaces (task_id, branch, workspace_path) VALUES (?, ?, ?)",
        (task_id, branch, str(path)),
    )
    conn.commit()
    conn.close()


def _workspace_branches(db_path, task_id):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT branch FROM task_workspaces WHERE task_id = ?", (task_id,)
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_reconciles_safe_siblings(repo_with_origin):
    repo = repo_with_origin["repo"]
    db_path = repo_with_origin["db_path"]

    # Sibling A: stale registry row — worktree path gone, branch at main tip
    # (fully merged into origin/main).
    _git(repo, "branch", "feature/TASK-527-stale", "main")
    _insert_workspace(db_path, 527, "feature/TASK-527-stale", repo / "gone-A")

    # Sibling B: live worktree, branch off main with no commits (merged/empty).
    wt_b = repo.parent / "wt-B"
    _git(repo, "worktree", "add", str(wt_b), "-b", "feature/TASK-527-empty", "main")
    _insert_workspace(db_path, 527, "feature/TASK-527-empty", wt_b)

    out = io.StringIO()
    with redirect_stderr(out):
        ok = tusk_merge._reconcile_duplicate_task_workspaces(
            db_path, 527, "feature/TASK-527-kept", "main"
        )

    assert ok is True
    # Both sibling rows forgotten.
    assert _workspace_branches(db_path, 527) == set()
    # Both branches deleted.
    assert not _branch_exists(repo, "feature/TASK-527-stale")
    assert not _branch_exists(repo, "feature/TASK-527-empty")
    # Live worktree directory removed.
    assert not wt_b.exists()


def test_preserves_unmerged_sibling(repo_with_origin):
    repo = repo_with_origin["repo"]
    db_path = repo_with_origin["db_path"]

    # Sibling with a live worktree holding a commit NOT on origin/main.
    wt = repo.parent / "wt-unmerged"
    _git(repo, "worktree", "add", str(wt), "-b", "feature/TASK-527-work", "main")
    (wt / "new.txt").write_text("unmerged work\n")
    _git(wt, "add", "new.txt")
    _git(wt, "commit", "-m", "unmerged commit")
    _insert_workspace(db_path, 527, "feature/TASK-527-work", wt)

    out = io.StringIO()
    with redirect_stderr(out):
        ok = tusk_merge._reconcile_duplicate_task_workspaces(
            db_path, 527, "feature/TASK-527-kept", "main"
        )

    assert ok is False
    # Row, worktree, and branch all preserved — no work lost.
    assert _workspace_branches(db_path, 527) == {"feature/TASK-527-work"}
    assert wt.exists()
    assert _branch_exists(repo, "feature/TASK-527-work")
    # Targeted remediation surfaced.
    stderr = out.getvalue()
    assert "leaving it intact" in stderr
    assert "git worktree remove" in stderr


def test_no_siblings_is_noop(repo_with_origin):
    db_path = repo_with_origin["db_path"]
    # Only the merged branch's own row exists (or none) — nothing to reconcile.
    _insert_workspace(db_path, 527, "feature/TASK-527-kept", repo_with_origin["repo"] / "kept")
    ok = tusk_merge._reconcile_duplicate_task_workspaces(
        db_path, 527, "feature/TASK-527-kept", "main"
    )
    assert ok is True
    # The kept row is left untouched (it is the merged branch, excluded).
    assert _workspace_branches(db_path, 527) == {"feature/TASK-527-kept"}
