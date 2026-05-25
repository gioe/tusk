"""Integration tests for `tusk task-worktree reconcile` (TASK-476).

Reconcile drops registry rows whose task is Done, whose feature branch is
fully merged into the default branch, and whose worktree is clean. Dirty
worktrees and unmerged branches are refused — those are legitimate
in-progress work that the prune path won't touch either.
"""

import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, *, cwd, env, stdin=None):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=stdin,
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


def _repo_with_tusk(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    # Disable auto-prune so test setup that intentionally leaves stale rows is
    # not silently wiped by a subsequent create.
    env["TUSK_NO_AUTO_PRUNE"] = "1"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")
    monkeypatch.setenv("TUSK_NO_AUTO_PRUNE", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return repo, db_path, env


def _insert_task(db_path, summary="reconcile task"):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score) "
            "VALUES (?, 'reconcile body', 'To Do', 'feature', 'High', 'M', 30)",
            (summary,),
        )
        conn.commit()
        return cur.lastrowid


def _mark_task_done(db_path, task_id):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'Done', closed_reason = 'completed', "
            "closed_at = datetime('now') WHERE id = ?",
            (task_id,),
        )
        conn.commit()


def _create_workspace(repo, env, task_id, slug, workspace_root):
    result = _run(
        [
            "task-worktree",
            "create",
            str(task_id),
            slug,
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _commit_in_worktree(payload, filename, body):
    path = os.path.join(payload["workspace_path"], filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    _git(["add", filename], cwd=payload["workspace_path"])
    _git(["commit", "-m", f"[TASK-{payload['task_id']}] {filename}"],
         cwd=payload["workspace_path"])


def _merge_branch_into_main(repo, branch):
    _git(["checkout", "main"], cwd=repo)
    _git(["merge", "--ff-only", branch], cwd=repo)


def test_finds_eligible(tmp_path, monkeypatch):
    """Eligible worktree (Done + merged + clean) is identified and cleaned up."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task_id = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    payload = _create_workspace(repo, env, task_id, "eligible", workspace_root)
    _commit_in_worktree(payload, "feature.txt", "feature body\n")
    _merge_branch_into_main(repo, payload["branch"])
    _mark_task_done(db_path, task_id)

    result = _run(
        ["task-worktree", "reconcile", "--yes", "--format", "json"],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload_out = json.loads(result.stdout)
    assert payload_out["dry_run"] is False
    assert len(payload_out["eligible"]) == 1
    assert payload_out["eligible"][0]["task_id"] == task_id
    assert payload_out["removed_count"] == 1
    assert payload_out["results"][0]["ok"] is True

    # Registry row, worktree dir, and branch should all be gone now.
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM task_workspaces WHERE task_id = ?", (task_id,)
        ).fetchall()
    assert rows == []
    assert not os.path.isdir(payload["workspace_path"])
    branches = _git(["branch", "--list", payload["branch"]], cwd=repo).stdout
    assert branches.strip() == ""


def test_refuses_dirty(tmp_path, monkeypatch):
    """A dirty worktree (uncommitted changes) is reported skipped, not touched."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task_id = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    payload = _create_workspace(repo, env, task_id, "dirty", workspace_root)
    _commit_in_worktree(payload, "feature.txt", "feature body\n")
    _merge_branch_into_main(repo, payload["branch"])
    _mark_task_done(db_path, task_id)

    # Dirty the worktree with an uncommitted edit.
    dirty_path = os.path.join(payload["workspace_path"], "dirty.txt")
    with open(dirty_path, "w", encoding="utf-8") as f:
        f.write("uncommitted change\n")

    result = _run(
        ["task-worktree", "reconcile", "--yes", "--format", "json"],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload_out = json.loads(result.stdout)
    assert payload_out["eligible"] == []
    assert len(payload_out["skipped"]) == 1
    skipped = payload_out["skipped"][0]
    assert skipped["task_id"] == task_id
    assert skipped["clean"] is False
    assert any("not clean" in r for r in skipped["skip_reasons"])

    # Worktree dir, branch, and registry row all still present.
    assert os.path.isdir(payload["workspace_path"])
    branches = _git(["branch", "--list", payload["branch"]], cwd=repo).stdout
    assert payload["branch"] in branches
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM task_workspaces WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert count == 1


def test_refuses_unmerged(tmp_path, monkeypatch):
    """A worktree whose branch is NOT merged into default is reported skipped."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task_id = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    payload = _create_workspace(repo, env, task_id, "unmerged", workspace_root)
    _commit_in_worktree(payload, "unmerged.txt", "still in progress\n")
    _mark_task_done(db_path, task_id)
    # Intentionally NOT merged into main.

    result = _run(
        ["task-worktree", "reconcile", "--yes", "--format", "json"],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload_out = json.loads(result.stdout)
    assert payload_out["eligible"] == []
    assert len(payload_out["skipped"]) == 1
    skipped = payload_out["skipped"][0]
    assert skipped["merged_into_default"] is False
    assert any("not fully merged" in r for r in skipped["skip_reasons"])

    # Nothing was destroyed.
    assert os.path.isdir(payload["workspace_path"])
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM task_workspaces WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert count == 1


def test_dry_run(tmp_path, monkeypatch):
    """--dry-run lists the plan but does NOT remove anything."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task_id = _insert_task(db_path)
    workspace_root = tmp_path / "workspaces"

    payload = _create_workspace(repo, env, task_id, "dryrun", workspace_root)
    _commit_in_worktree(payload, "feature.txt", "feature body\n")
    _merge_branch_into_main(repo, payload["branch"])
    _mark_task_done(db_path, task_id)

    result = _run(
        [
            "task-worktree",
            "reconcile",
            "--dry-run",
            "--yes",
            "--format",
            "json",
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload_out = json.loads(result.stdout)
    assert payload_out["dry_run"] is True
    assert len(payload_out["eligible"]) == 1
    # No removals performed.
    assert payload_out["removed_count"] == 0
    assert payload_out["results"] == []

    # Nothing actually changed.
    assert os.path.isdir(payload["workspace_path"])
    branches = _git(["branch", "--list", payload["branch"]], cwd=repo).stdout
    assert payload["branch"] in branches
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM task_workspaces WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert count == 1


def test_json_format(tmp_path, monkeypatch):
    """--format json emits a structured envelope with expected top-level keys."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    eligible_task = _insert_task(db_path, summary="eligible")
    dirty_task = _insert_task(db_path, summary="dirty")
    workspace_root = tmp_path / "workspaces"

    # One eligible row.
    eligible_payload = _create_workspace(
        repo, env, eligible_task, "json-elig", workspace_root
    )
    _commit_in_worktree(eligible_payload, "feature.txt", "body\n")
    _merge_branch_into_main(repo, eligible_payload["branch"])
    _mark_task_done(db_path, eligible_task)

    # One ineligible row (dirty). Use a distinct filename so the commit lands
    # — branching off main after the eligible merge would otherwise re-introduce
    # `feature.txt` with identical content and produce an empty commit.
    dirty_payload = _create_workspace(
        repo, env, dirty_task, "json-dirty", workspace_root
    )
    _commit_in_worktree(dirty_payload, "dirty-feature.txt", "body\n")
    _merge_branch_into_main(repo, dirty_payload["branch"])
    _mark_task_done(db_path, dirty_task)
    with open(
        os.path.join(dirty_payload["workspace_path"], "dirty.txt"), "w",
        encoding="utf-8",
    ) as f:
        f.write("uncommitted\n")

    result = _run(
        [
            "task-worktree",
            "reconcile",
            "--dry-run",
            "--yes",
            "--format",
            "json",
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload_out = json.loads(result.stdout)

    expected_keys = {
        "dry_run",
        "default_branch",
        "eligible",
        "skipped",
        "removed_count",
        "results",
    }
    assert expected_keys.issubset(payload_out.keys())
    assert payload_out["default_branch"] == "main"
    assert len(payload_out["eligible"]) == 1
    assert payload_out["eligible"][0]["task_id"] == eligible_task
    assert len(payload_out["skipped"]) == 1
    assert payload_out["skipped"][0]["task_id"] == dirty_task

    # Each eligible row carries the per-row classification fields.
    elig = payload_out["eligible"][0]
    for key in (
        "task_status",
        "branch_present",
        "merged_into_default",
        "clean",
        "eligible",
        "skip_reasons",
    ):
        assert key in elig, f"missing {key} in eligible row"
    assert elig["task_status"] == "Done"
    assert elig["merged_into_default"] is True
    assert elig["clean"] is True
    assert elig["eligible"] is True
    assert elig["skip_reasons"] == []
