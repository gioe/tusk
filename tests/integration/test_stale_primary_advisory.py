"""Integration test for the stale-primary advisory at task-worktree create
time (issue #913).

When primary lags ``origin/<default>`` by N commits, PATH-resolved tusk
invocations from inside a task worktree run primary's stale ``bin/tusk``
against the worktree's CWD — the silent-MANIFEST-corruption vector. The
new advisory in ``_maybe_advise_stale_primary`` surfaces the staleness
at create time so the operator can run ``tusk sync-main`` first.
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


def _seed_repo_with_origin(tmp_path, *, advance_origin: bool):
    """Build primary + bare origin. When ``advance_origin`` is True, push
    an extra commit through a second clone so primary lags origin by 1.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-b", "main", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    _git(["remote", "add", "origin", str(origin)], cwd=primary)
    (primary / "README.md").write_text("x\n", encoding="utf-8")
    _git(["add", "."], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)
    _git(["push", "-u", "origin", "main"], cwd=primary)
    # Set origin/HEAD so symbolic-ref resolves to main.
    _git(["remote", "set-head", "origin", "main"], cwd=primary)

    if advance_origin:
        advancer = tmp_path / "advancer"
        _git(["clone", str(origin), str(advancer)], cwd=tmp_path)
        _git(["config", "user.email", "tusk@example.test"], cwd=advancer)
        _git(["config", "user.name", "Tusk Tests"], cwd=advancer)
        (advancer / "advance.txt").write_text("y\n", encoding="utf-8")
        _git(["add", "."], cwd=advancer)
        _git(["commit", "-m", "advance origin"], cwd=advancer)
        _git(["push", "origin", "main"], cwd=advancer)

    return primary


def _init_tusk(primary, monkeypatch):
    db_path = primary / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    env.pop("TUSK_NO_STALE_PRIMARY_ADVISORY", None)
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=primary, env=env)
    assert result.returncode == 0, result.stderr
    return db_path, env


def _insert_task(db_path, description="Update README.md"):
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "('stale-primary test', ?, 'In Progress', 'feature', 'High', 'M', 30)",
            (description,),
        )
        conn.commit()
        return cur.lastrowid


def test_stale_primary_triggers_advisory(tmp_path, monkeypatch):
    """When primary is 1 commit behind origin/main, task-worktree create
    must emit a stderr advisory naming the count and the recovery
    command. This is the issue #913 acceptance test.
    """
    primary = _seed_repo_with_origin(tmp_path, advance_origin=True)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "stale-primary-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    # Issue #913's literal grep:  echo "$WT_OUT" | grep -qi "behind origin"
    assert "behind origin" in result.stderr.lower(), (
        f"stderr should contain 'behind origin' advisory; got: {result.stderr}"
    )
    # And the recovery command must be named.
    assert "tusk sync-main" in result.stderr, (
        f"stderr should name the recovery command; got: {result.stderr}"
    )


def test_uptodate_primary_no_advisory(tmp_path, monkeypatch):
    """When primary is at origin/<default>, no stale-primary advisory fires."""
    primary = _seed_repo_with_origin(tmp_path, advance_origin=False)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "uptodate-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "behind origin" not in result.stderr.lower(), (
        f"no advisory should fire when primary is up-to-date; got: "
        f"{result.stderr}"
    )


def test_env_var_suppresses_advisory(tmp_path, monkeypatch):
    """TUSK_NO_STALE_PRIMARY_ADVISORY=1 silences the advisory even when
    primary is genuinely behind origin."""
    primary = _seed_repo_with_origin(tmp_path, advance_origin=True)
    db_path, env = _init_tusk(primary, monkeypatch)
    env["TUSK_NO_STALE_PRIMARY_ADVISORY"] = "1"
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "envoff-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "behind origin" not in result.stderr.lower(), (
        f"env var should silence the advisory; got: {result.stderr}"
    )


def test_no_origin_remote_silent(tmp_path, monkeypatch):
    """When the repo has no ``origin`` remote (offline / fresh init),
    the advisory must silently no-op rather than block worktree creation."""
    # Build a primary repo without any remote.
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(["init", "-b", "main"], cwd=primary)
    _git(["config", "user.email", "tusk@example.test"], cwd=primary)
    _git(["config", "user.name", "Tusk Tests"], cwd=primary)
    (primary / "README.md").write_text("x\n", encoding="utf-8")
    _git(["add", "."], cwd=primary)
    _git(["commit", "-m", "initial"], cwd=primary)

    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "noorigin-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    # Without origin/HEAD the advisory can't compute the count — must
    # silently no-op rather than error or emit a misleading message.
    assert "behind origin" not in result.stderr.lower(), (
        f"no advisory should fire when origin remote is absent; got: "
        f"{result.stderr}"
    )
