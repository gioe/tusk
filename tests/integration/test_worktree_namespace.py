"""Integration tests for per-repo namespacing of task worktrees (TASK-468).

`tusk task-worktree create` writes new worktrees under
``<workspace_root>/<namespace>/TASK-<id>-<slug>/`` where ``namespace`` is the
primary-repo basename, falling back to ``<basename>-<sha256(repo)[:6]>`` when
another repo with the same basename has already claimed the namespace dir.
A ``.tusk-primary`` marker file records the owning repo so the second create
for the same repo is O(1). Pre-existing flat-pool registry rows (workspaces
written directly under ``workspace_root`` without a namespace component)
remain valid and are not touched.
"""

import hashlib
import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
PRIMARY_MARKER = ".tusk-primary"


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


def _repo_with_tusk(tmp_path, monkeypatch, *, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("namespace test\n", encoding="utf-8")
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


def _insert_task(db_path, *, summary="namespace task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score) "
            "VALUES (?, 'namespace test', 'To Do', 'feature', 'High', 'M', 30)",
            (summary,),
        )
        conn.commit()
        return cur.lastrowid


def test_default_basename(tmp_path, monkeypatch):
    """With no collision, namespace equals os.path.basename(repo_root)."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="alpha")
    workspace_root = tmp_path / "workspaces"
    task_id = _insert_task(db_path)

    result = _run(
        [
            "task-worktree", "create", str(task_id), "default-ns",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    expected = str(workspace_root / "alpha" / f"TASK-{task_id}-default-ns")
    assert payload["workspace_path"] == expected, (
        f"expected workspace under <root>/alpha/, got {payload['workspace_path']}"
    )
    assert os.path.isdir(payload["workspace_path"])
    # The namespace dir was created and exists for future calls.
    assert (workspace_root / "alpha").is_dir()


def test_collision_fallback(tmp_path, monkeypatch):
    """A namespace subdir claimed by another repo forces the hash fallback."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="repo")
    workspace_root = tmp_path / "workspaces"
    task_id = _insert_task(db_path)

    # Pre-stake the basename namespace as if a different repo got there first.
    collision_dir = workspace_root / "repo"
    collision_dir.mkdir(parents=True)
    other_repo = str(tmp_path / "other-project")
    (collision_dir / PRIMARY_MARKER).write_text(other_repo + "\n", encoding="utf-8")

    result = _run(
        [
            "task-worktree", "create", str(task_id), "collide",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    digest = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:6]
    expected_ns = f"repo-{digest}"
    expected = str(workspace_root / expected_ns / f"TASK-{task_id}-collide")
    assert payload["workspace_path"] == expected, (
        f"expected fallback under {expected_ns!r}, got {payload['workspace_path']}"
    )
    # The pre-existing collision marker was not overwritten.
    marker_text = (collision_dir / PRIMARY_MARKER).read_text(encoding="utf-8").strip()
    assert marker_text == other_repo, (
        f"collision marker was overwritten: {marker_text!r}"
    )


def test_marker_reuse(tmp_path, monkeypatch):
    """First create writes the marker; second create reuses the same namespace dir."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="repo")
    workspace_root = tmp_path / "workspaces"
    first_task = _insert_task(db_path, summary="first")
    second_task = _insert_task(db_path, summary="second")

    first = _run(
        [
            "task-worktree", "create", str(first_task), "first",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo, env=env,
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    marker_path = workspace_root / "repo" / PRIMARY_MARKER
    assert marker_path.is_file(), (
        f"expected .tusk-primary marker at {marker_path}, "
        f"namespace dir contents: {list((workspace_root / 'repo').iterdir())}"
    )
    assert marker_path.read_text(encoding="utf-8").strip() == str(repo)

    second = _run(
        [
            "task-worktree", "create", str(second_task), "second",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo, env=env,
    )
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)

    # Both worktrees live under the SAME namespace dir (no fallback fired).
    expected_ns_dir = str(workspace_root / "repo")
    assert os.path.dirname(first_payload["workspace_path"]) == expected_ns_dir
    assert os.path.dirname(second_payload["workspace_path"]) == expected_ns_dir
    # And the marker file is still there with the same contents — second
    # create read it and short-circuited rather than rewriting.
    assert marker_path.read_text(encoding="utf-8").strip() == str(repo)


def test_flat_pool_compat(tmp_path, monkeypatch):
    """Pre-existing flat-pool rows still resolve via list and survive prune."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch, name="repo")
    workspace_root = tmp_path / "workspaces"
    flat_task = _insert_task(db_path, summary="flat-pool legacy")

    # Simulate the legacy flat-pool layout: workspace_path lives directly
    # under workspace_root with no namespace component, and the worktree is
    # a real git worktree so neither disk nor `git worktree list` flags it
    # as stale.
    workspace_root.mkdir(parents=True, exist_ok=True)
    flat_path = workspace_root / f"TASK-{flat_task}-legacy"
    flat_branch = f"feature/TASK-{flat_task}-legacy"
    _git(
        ["worktree", "add", "-b", flat_branch, str(flat_path), "main"],
        cwd=repo,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (flat_task, flat_branch, str(flat_path)),
        )
        conn.commit()

    # task-worktree list reports the row with the legacy path intact.
    listed = _run(
        ["task-worktree", "list", "--format", "json"],
        cwd=repo, env=env,
    )
    assert listed.returncode == 0, listed.stderr
    rows = json.loads(listed.stdout)
    flat_row = next((r for r in rows if r["task_id"] == flat_task), None)
    assert flat_row is not None, f"flat-pool row missing from list: {rows}"
    assert flat_row["workspace_path"] == str(flat_path)
    assert flat_row["exists_on_disk"] is True
    assert flat_row["live_workspace_path"] == str(flat_path)

    # task-worktree prune (dry-run) does NOT flag the live legacy row as stale.
    pruned = _run(
        ["task-worktree", "prune", "--dry-run", "--format", "json"],
        cwd=repo, env=env,
    )
    assert pruned.returncode == 0, pruned.stderr
    plan = json.loads(pruned.stdout)
    removed_ids = [r["task_id"] for r in plan["removed"]]
    assert flat_task not in removed_ids, (
        f"prune should leave the live flat-pool row alone; got removed={removed_ids}"
    )
    assert plan["removed_count"] == 0
