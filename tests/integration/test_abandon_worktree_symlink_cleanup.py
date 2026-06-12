"""Integration test for ``tusk abandon``'s pre-clean of tusk-created
auto-symlinks before ``git worktree remove`` (issue #910).

``tusk abandon`` imports ``_remove_recorded_task_worktree`` from
``bin/tusk-merge.py``, so the same pre-clean covers both surfaces.
This test exercises the abandon path against a worktree whose only
"dirty" state is the canonical-fallback ``.venv`` auto-symlink.
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


def _seed_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("v\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n", encoding="utf-8")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(["add", ".gitignore", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    env.pop("TUSK_NO_AUTO_SYMLINK", None)
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    return repo, db_path, env


def test_abandon_completed_cleans_canonical_fallback_symlinks(tmp_path, monkeypatch):
    """``tusk abandon --reason completed`` on a no-commit task whose only
    dirty state in the worktree is the canonical-fallback ``.venv`` symlink
    must succeed without manual ``git worktree remove --force`` (issue #910).
    """
    repo, db_path, env = _seed_repo(tmp_path, monkeypatch)

    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "('abandon test', 'completed via DB write', 'In Progress', "
            "'feature', 'High', 'M', 30)"
        )
        conn.commit()
        task_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) "
            "VALUES (?, datetime('now'))",
            (task_id,),
        )
        conn.commit()
        session_id = cur.lastrowid

    workspace_root = tmp_path / "workspaces"
    create = _run(
        [
            "task-worktree", "create",
            str(task_id), "abandontest",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert create.returncode == 0, create.stderr
    payload = json.loads(create.stdout)
    wt = payload["workspace_path"]
    assert os.path.islink(os.path.join(wt, ".venv")), (
        "canonical-fallback .venv symlink must be present pre-abandon"
    )

    # Abandon as completed (no commits). The pre-clean must remove the
    # .venv symlink and `git worktree remove` should succeed.
    abandon = _run(
        [
            "abandon", str(task_id),
            "--reason", "completed",
            "--session", str(session_id),
            "--note", "DB-only deliverable; no commits expected",
        ],
        cwd=repo,
        env=env,
    )
    assert abandon.returncode == 0, (
        f"tusk abandon should succeed when only dirty state is the "
        f".venv symlink; stdout={abandon.stdout} stderr={abandon.stderr}"
    )
    assert not os.path.exists(wt), (
        f"worktree {wt} should be removed after successful abandon"
    )

    # Task is Done with reason=completed.
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT status, closed_reason FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row == ("Done", "completed"), (
        f"task should be Done/completed after abandon; got {row}"
    )


def _insert_task_with_session(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "('abandon sweep test', 'completed via DB write', 'In Progress', "
            "'feature', 'High', 'M', 30)"
        )
        conn.commit()
        task_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) "
            "VALUES (?, datetime('now'))",
            (task_id,),
        )
        conn.commit()
        session_id = cur.lastrowid
    return task_id, session_id


def test_abandon_sweeps_unconfigured_primary_targeting_symlink(
    tmp_path, monkeypatch
):
    """An untracked symlink at a location no config entry names (the issue
    #1067 test-gate shape) blocks the first ``git worktree remove``; the
    failure-path sweep must unlink it (its target resolves inside the
    primary) and the retry must succeed, so abandon exits 0 (issue #1077).
    """
    repo, db_path, env = _seed_repo(tmp_path, monkeypatch)
    task_id, session_id = _insert_task_with_session(db_path)

    workspace_root = tmp_path / "workspaces"
    create = _run(
        [
            "task-worktree", "create",
            str(task_id), "sweeptest",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert create.returncode == 0, create.stderr
    wt = json.loads(create.stdout)["workspace_path"]

    # Not gitignored, not in worktree.symlink_files, not a canonical name —
    # the name-based pre-clean cannot match it. Targets the primary checkout.
    stray = os.path.join(wt, "sdk")
    os.symlink(os.path.join(str(repo), ".venv"), stray)

    abandon = _run(
        [
            "abandon", str(task_id),
            "--reason", "completed",
            "--session", str(session_id),
            "--note", "sweep-retry regression (issue #1077)",
        ],
        cwd=repo,
        env=env,
    )
    assert abandon.returncode == 0, (
        f"abandon should sweep the primary-targeting symlink and retry; "
        f"stdout={abandon.stdout} stderr={abandon.stderr}"
    )
    assert "retrying git worktree remove" in abandon.stderr
    assert not os.path.exists(wt), (
        f"worktree {wt} should be removed after sweep + retry"
    )
    # The symlink target inside the primary survives.
    assert (repo / ".venv" / "marker").exists()


def test_abandon_still_blocks_on_real_untracked_file(tmp_path, monkeypatch):
    """A real untracked file is NOT tusk-shaped: the sweep must leave it
    alone, abandon must still fail worktree cleanup, and the diagnostic must
    name the leftover entry instead of an opaque git one-liner.
    """
    repo, db_path, env = _seed_repo(tmp_path, monkeypatch)
    task_id, session_id = _insert_task_with_session(db_path)

    workspace_root = tmp_path / "workspaces"
    create = _run(
        [
            "task-worktree", "create",
            str(task_id), "blocktest",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert create.returncode == 0, create.stderr
    wt = json.loads(create.stdout)["workspace_path"]

    stray = os.path.join(wt, "stray.txt")
    with open(stray, "w", encoding="utf-8") as f:
        f.write("real user data\n")

    abandon = _run(
        [
            "abandon", str(task_id),
            "--reason", "completed",
            "--session", str(session_id),
        ],
        cwd=repo,
        env=env,
    )
    assert abandon.returncode == 2, (
        f"abandon must refuse cleanup when a real untracked file remains; "
        f"stdout={abandon.stdout} stderr={abandon.stderr}"
    )
    assert "Remaining entries:" in abandon.stderr
    assert "stray.txt" in abandon.stderr
    assert os.path.exists(stray), "real untracked file must be preserved"
