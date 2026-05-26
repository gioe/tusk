"""Integration tests for ``tusk task-worktree relocate`` (TASK-479).

`tusk task-worktree relocate` migrates flat-pool registry rows (workspace_path
directly under workspace_root) into the per-repo namespaced layout introduced
by TASK-468 (workspace_root/<namespace>/TASK-<id>-<slug>). The command operates
on the current repo's registry only — there is no cross-repo coordination — and
is opt-in (never invoked automatically by `tusk upgrade`).
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
PRIMARY_MARKER = ".tusk-primary"


def _run(args, *, cwd, env, input_text=None):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        input=input_text,
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


def _repo_with_tusk(tmp_path, monkeypatch, *, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("relocate test\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return repo, db_path, env


def _insert_task(db_path, *, summary="relocate task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) "
            "VALUES (?, 'relocate test', 'To Do', 'feature', 'High', 'M', 30)",
            (summary,),
        )
        conn.commit()
        return cur.lastrowid


def _make_flat_pool_worktree(repo, db_path, workspace_root, task_id, slug):
    """Create a real flat-pool worktree + matching registry row.

    Returns ``(workspace_path, branch)``. The worktree is a real git worktree
    so neither `git worktree list` nor on-disk checks flag it as stale.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace_path = workspace_root / f"TASK-{task_id}-{slug}"
    branch = f"feature/TASK-{task_id}-{slug}"
    _git(
        ["worktree", "add", "-b", branch, str(workspace_path), "main"],
        cwd=repo,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task_id, branch, str(workspace_path)),
        )
        conn.commit()
    return workspace_path, branch


def test_moves_to_namespace(tmp_path, monkeypatch):
    """relocate moves a flat-pool worktree under workspace_root/<basename>/."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    task_id = _insert_task(db_path)
    flat_path, branch = _make_flat_pool_worktree(
        repo, db_path, workspace_root, task_id, "move-test"
    )

    result = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    expected_new = workspace_root / "alpha" / f"TASK-{task_id}-move-test"
    assert payload["namespace"] == "alpha"
    assert payload["dry_run"] is False
    assert len(payload["results"]) == 1
    res = payload["results"][0]
    assert res["task_id"] == task_id
    assert res["ok"] is True
    assert res["new_path"] == str(expected_new)
    assert res["old_path"] == str(flat_path)

    # New path exists; old path is gone.
    assert expected_new.is_dir(), f"expected new path to exist: {expected_new}"
    assert not flat_path.exists(), f"flat-pool path should be removed: {flat_path}"

    # The marker file was written when relocate claimed the namespace dir.
    marker = workspace_root / "alpha" / PRIMARY_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == str(repo)

    # git worktree list reflects the new path.
    listed = _git(["worktree", "list", "--porcelain"], cwd=repo)
    assert str(expected_new) in listed.stdout, (
        f"new path missing from `git worktree list`:\n{listed.stdout}"
    )
    assert str(flat_path) not in listed.stdout

    # Branch still resolves at the same ref (move is non-destructive).
    branch_check = _git(["rev-parse", "--verify", branch], cwd=repo)
    assert branch_check.returncode == 0


def test_updates_registry(tmp_path, monkeypatch):
    """After a successful relocate, the registry row points at the new path."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    task_id = _insert_task(db_path)
    flat_path, branch = _make_flat_pool_worktree(
        repo, db_path, workspace_root, task_id, "registry"
    )

    result = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr

    expected_new = str(workspace_root / "alpha" / f"TASK-{task_id}-registry")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT workspace_path, branch FROM task_workspaces WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None, "registry row missing after relocate"
    assert row[0] == expected_new, (
        f"registry workspace_path not updated: got {row[0]!r}, "
        f"expected {expected_new!r}"
    )
    # Branch column is unchanged — relocate only moves the path.
    assert row[1] == branch


def test_prune_first(tmp_path, monkeypatch):
    """relocate drops stale rows before planning the move."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    stale_task = _insert_task(db_path, summary="stale")
    live_task = _insert_task(db_path, summary="live")

    # Live flat-pool worktree we expect to be relocated.
    _make_flat_pool_worktree(repo, db_path, workspace_root, live_task, "live")

    # Stale row: registry entry survives but its workspace_path is missing on
    # disk AND not in `git worktree list`, AND its branch is gone — the same
    # predicate `tusk task-worktree prune` uses.
    stale_path, stale_branch = _make_flat_pool_worktree(
        repo, db_path, workspace_root, stale_task, "stale"
    )
    _git(["worktree", "remove", "--force", str(stale_path)], cwd=repo)
    _git(["branch", "-D", stale_branch], cwd=repo)

    result = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["pruned_count"] == 1, (
        f"expected one row pruned before relocate, got "
        f"pruned_count={payload['pruned_count']}"
    )

    with sqlite3.connect(db_path) as conn:
        task_ids = sorted(
            r[0] for r in conn.execute(
                "SELECT task_id FROM task_workspaces"
            ).fetchall()
        )
    assert stale_task not in task_ids, (
        f"stale row should have been pruned: got task_ids={task_ids}"
    )
    assert live_task in task_ids


def test_dry_run(tmp_path, monkeypatch):
    """--dry-run prints the plan without moving anything or pruning."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    stale_task = _insert_task(db_path, summary="stale")
    live_task = _insert_task(db_path, summary="live")

    flat_path, _ = _make_flat_pool_worktree(
        repo, db_path, workspace_root, live_task, "dryrun"
    )

    # Plant a stale row to verify dry-run also leaves prune candidates alone.
    stale_path, stale_branch = _make_flat_pool_worktree(
        repo, db_path, workspace_root, stale_task, "stale"
    )
    _git(["worktree", "remove", "--force", str(stale_path)], cwd=repo)
    _git(["branch", "-D", stale_branch], cwd=repo)

    result = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--dry-run", "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["dry_run"] is True
    assert payload["results"] == [], (
        f"dry-run must not produce results entries; got {payload['results']!r}"
    )
    expected_new = str(workspace_root / "alpha" / f"TASK-{live_task}-dryrun")
    plan_actions = {p["task_id"]: p for p in payload["plan"]}
    assert plan_actions[live_task]["action"] == "move"
    assert plan_actions[live_task]["new_path"] == expected_new
    # pruned_count still reflects what WOULD be pruned (for visibility) but the
    # row itself is left in the registry.
    assert payload["pruned_count"] == 1

    # Filesystem unchanged: old path still present, new path absent.
    assert flat_path.is_dir(), "dry-run should not move the worktree"
    assert not (workspace_root / "alpha" / f"TASK-{live_task}-dryrun").exists()
    # Namespace dir + marker NOT created under dry-run.
    assert not (workspace_root / "alpha").exists(), (
        "dry-run should not create the namespace directory"
    )

    # Registry unchanged: both rows survive, live row still points at flat path.
    with sqlite3.connect(db_path) as conn:
        rows = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT task_id, workspace_path FROM task_workspaces"
            ).fetchall()
        }
    assert stale_task in rows, "dry-run should not have pruned the stale row"
    assert rows[live_task] == str(flat_path)


def test_skips_dirty(tmp_path, monkeypatch):
    """A dirty worktree is skipped with a reason; its registry row is unchanged."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    task_id = _insert_task(db_path)
    flat_path, _ = _make_flat_pool_worktree(
        repo, db_path, workspace_root, task_id, "dirty"
    )

    # Make the worktree dirty.
    (flat_path / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

    result = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # No move attempted.
    assert payload["results"] == [], (
        f"dirty worktree should not have been moved; got {payload['results']!r}"
    )
    plan_actions = {p["task_id"]: p for p in payload["plan"]}
    assert plan_actions[task_id]["action"] == "skip"
    assert "dirty" in plan_actions[task_id]["reason"].lower()

    # Filesystem unchanged.
    assert flat_path.is_dir()
    assert (flat_path / "uncommitted.txt").is_file()
    assert not (workspace_root / "alpha" / f"TASK-{task_id}-dirty").exists()

    # Registry row still points at the original flat path.
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT workspace_path FROM task_workspaces WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row[0] == str(flat_path)


def test_idempotent(tmp_path, monkeypatch):
    """A second relocate pass treats already-namespaced rows as no-ops."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    task_id = _insert_task(db_path)
    _make_flat_pool_worktree(repo, db_path, workspace_root, task_id, "idempotent")

    first = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert len(first_payload["results"]) == 1
    assert first_payload["results"][0]["ok"] is True

    expected_new = str(workspace_root / "alpha" / f"TASK-{task_id}-idempotent")

    second = _run(
        [
            "task-worktree", "relocate",
            "--workspace-root", str(workspace_root),
            "--yes", "--format", "json",
        ],
        cwd=repo, env=env,
    )
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)

    # No move performed on the second pass.
    assert second_payload["results"] == [], (
        f"second relocate must be a no-op; got results={second_payload['results']!r}"
    )
    # The plan classifies the row as already_namespaced rather than move/skip.
    plan_actions = {p["task_id"]: p for p in second_payload["plan"]}
    assert plan_actions[task_id]["action"] == "already_namespaced", (
        f"expected already_namespaced; got {plan_actions[task_id]!r}"
    )

    # Filesystem + registry unchanged from the first run.
    assert os.path.isdir(expected_new)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT workspace_path FROM task_workspaces WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row[0] == expected_new
