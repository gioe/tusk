"""Regression tests for ``tusk scope add`` existence resolution (issue #1099).

``tusk scope add`` used to validate a plain scope path's existence against the
*primary* checkout that ``bin/tusk`` derives from the shared config path
(``TUSK_REPO_ROOT`` is exported as the primary even from a linked worktree).
When the primary lagged ``origin/<default>`` — common after several sequential
worktree tasks merge to main while the primary stays on an older branch — a
path already present on the task's worktree base was rejected with
``scope path does not exist at repo root``, and the suggested
``--source creates`` workaround mislabeled a *modified* file as *created*.

The fix resolves the existence check against the worktree the command runs in
(``_worktree_root`` → git toplevel of CWD), accepting a path that is either
materialized on disk in the worktree or tracked in the worktree's ``HEAD``.

These tests build a real primary checkout plus a linked ``git worktree`` so the
primary-vs-worktree divergence is faithful rather than mocked.
"""

import json
import os
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git(args, *, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def _seed_task(db, summary="worktree scope test"):
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) "
            "VALUES (?, '', 'To Do', 'bug', 'High', 'S', 20)",
            (summary,),
        )
        conn.commit()
        return cur.lastrowid


def _scope_rows(db, task_id):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT pattern, source FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _record_workspace(db, task_id, branch, workspace_path):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task_id, branch, str(workspace_path)),
        )
        conn.commit()


def _primary_with_tusk(tmp_path, monkeypatch):
    """Build a primary git checkout with a tusk DB and pin TUSK_DB to it."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "README.md").write_text("primary\n", encoding="utf-8")
    _git(["add", "."], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    db = primary / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db)
    env["TUSK_QUIET"] = "1"
    monkeypatch.setenv("TUSK_DB", str(db))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=primary, env=env)
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return primary, db, env


def _add_linked_worktree(primary, tmp_path, branch="feature"):
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-b", branch, str(wt)], cwd=primary)
    return wt


def test_scope_add_accepts_worktree_file_absent_from_primary(tmp_path, monkeypatch):
    """The reported bug: a file present on the worktree branch but not in the
    lagging primary checkout is accepted and recorded with the modify-
    appropriate implicit source — NOT forced to ``--source creates``."""
    primary, db, env = _primary_with_tusk(tmp_path, monkeypatch)
    wt = _add_linked_worktree(primary, tmp_path)

    # Commit a file that exists ONLY on the worktree's feature branch.
    (wt / "new_module.py").write_text("value = 1\n", encoding="utf-8")
    _git(["add", "new_module.py"], cwd=wt)
    _git(["commit", "-m", "add new_module on feature"], cwd=wt)

    # Sanity: the primary checkout (still on main) does not have it on disk.
    assert not (primary / "new_module.py").exists()

    task = _seed_task(db)
    added = _run(
        ["scope", "add", str(task), "new_module.py",
         "--reason", "modify a file the primary checkout does not yet have"],
        cwd=str(wt),
        env=env,
    )

    assert added.returncode == 0, added.stderr
    payload = json.loads(added.stdout)
    assert payload["pattern"] == "new_module.py"
    # Implicit source before any task work is operator_declared — the point is
    # that the operator did NOT have to lie with --source creates.
    assert payload["source"] == "operator_declared"
    assert {"pattern": "new_module.py", "source": "operator_declared"} in _scope_rows(db, task)


def test_scope_add_from_primary_uses_recorded_task_worktree(tmp_path, monkeypatch):
    """Issue #1149: operators often run ``tusk scope add`` from the primary
    checkout. In that mode, a recorded task worktree is the task's real file
    context; do not validate solely against the primary checkout's branch."""
    primary, db, env = _primary_with_tusk(tmp_path, monkeypatch)
    wt = _add_linked_worktree(primary, tmp_path)

    (wt / "worktree_only.py").write_text("value = 4\n", encoding="utf-8")
    _git(["add", "worktree_only.py"], cwd=wt)
    _git(["commit", "-m", "add worktree_only.py on feature"], cwd=wt)
    assert not (primary / "worktree_only.py").exists()

    task = _seed_task(db)
    _record_workspace(db, task, "feature", wt)
    added = _run(
        ["scope", "add", str(task), "worktree_only.py",
         "--reason", "operator invoked scope add from primary checkout"],
        cwd=str(primary),
        env=env,
    )

    assert added.returncode == 0, added.stderr
    payload = json.loads(added.stdout)
    assert payload["pattern"] == "worktree_only.py"
    assert payload["source"] == "operator_declared"
    assert {"pattern": "worktree_only.py", "source": "operator_declared"} in _scope_rows(db, task)


def test_scope_add_accepts_worktree_head_tracked_unmaterialized_file(tmp_path, monkeypatch):
    """A path tracked in the worktree's HEAD but not materialized on disk is
    still accepted — covers the ``_path_exists_for_scope`` git-HEAD fallback
    that keeps sparse-checkout scope additions working after the fix."""
    primary, db, env = _primary_with_tusk(tmp_path, monkeypatch)
    wt = _add_linked_worktree(primary, tmp_path)

    (wt / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    _git(["add", "tracked.py"], cwd=wt)
    _git(["commit", "-m", "add tracked.py on feature"], cwd=wt)
    # Remove it from the working tree so only the HEAD entry remains.
    os.remove(wt / "tracked.py")
    assert not (wt / "tracked.py").exists()

    task = _seed_task(db)
    added = _run(
        ["scope", "add", str(task), "tracked.py", "--reason", "tracked but not on disk"],
        cwd=str(wt),
        env=env,
    )

    assert added.returncode == 0, added.stderr
    assert json.loads(added.stdout)["pattern"] == "tracked.py"


def test_scope_add_rejects_path_absent_from_worktree_and_primary(tmp_path, monkeypatch):
    """A genuinely nonexistent path (not on disk, not tracked) is still
    rejected from a worktree with exit 2 and the existing message."""
    primary, db, env = _primary_with_tusk(tmp_path, monkeypatch)
    wt = _add_linked_worktree(primary, tmp_path)

    task = _seed_task(db)
    res = _run(
        ["scope", "add", str(task), "does/not/exist.py"],
        cwd=str(wt),
        env=env,
    )

    assert res.returncode == 2, res.stdout + res.stderr
    assert "does not exist" in res.stderr
    assert _scope_rows(db, task) == []


def test_scope_add_from_primary_checkout_unchanged(tmp_path, monkeypatch):
    """Backward compatibility: invoked from the primary checkout (CWD ==
    primary root), an existing file is accepted and a nonexistent one is
    rejected — identical to the pre-fix behavior."""
    primary, db, env = _primary_with_tusk(tmp_path, monkeypatch)
    (primary / "lib.py").write_text("y = 3\n", encoding="utf-8")
    _git(["add", "lib.py"], cwd=primary)
    _git(["commit", "-m", "add lib.py"], cwd=primary)

    task = _seed_task(db)
    ok = _run(["scope", "add", str(task), "lib.py"], cwd=str(primary), env=env)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["pattern"] == "lib.py"

    bad = _run(["scope", "add", str(task), "nope.py"], cwd=str(primary), env=env)
    assert bad.returncode == 2, bad.stdout + bad.stderr
    assert "does not exist" in bad.stderr
