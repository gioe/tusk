"""Integration tests for ``tusk merge`` pre-cleaning generated artifacts
before ``git worktree remove`` (issues #927/#919/#916/#1214).

When ``task-worktree create`` writes ``.venv`` / ``node_modules`` symlinks
into a new worktree (via ``worktree.symlink_files`` config or the canonical
fallback), ``git worktree remove`` refuses with "contains modified or
untracked files" because those symlinks are untracked. The pre-clean in
``_clean_tusk_auto_symlinks`` removes them before invoking the remove,
so merge cleanup succeeds without manual ``--force``.
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
    # Bare origin so `tusk merge`'s no-checkout push path has somewhere to
    # push (primary's main is "checked out in another worktree" from the
    # task worktree's perspective).
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-b", "main", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    _git(["remote", "add", "origin", str(origin)], cwd=repo)
    # Create a fake .venv directory in the primary so the symlink target exists.
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("v\n", encoding="utf-8")
    # Gitignore so it doesn't show in the index.
    (repo / ".gitignore").write_text(
        ".venv/\nnode_modules/\n.pytest_cache/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(["add", ".gitignore", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    _git(["push", "-u", "origin", "main"], cwd=repo)

    db_path = repo / "tusk" / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    # Use the canonical fallback path so the test doesn't depend on a config
    # that may or may not seed worktree.symlink_files.
    env.pop("TUSK_NO_AUTO_SYMLINK", None)
    monkeypatch.setenv("TUSK_DB", str(db_path))
    monkeypatch.setenv("TUSK_QUIET", "1")

    result = _run(["init", "--force", "--skip-gitignore"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"tusk init failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return repo, db_path, env


def _insert_task_and_start_session(db_path, description):
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "('symlink test', ?, 'In Progress', 'feature', 'High', 'M', 30)",
            (description,),
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


def test_merge_cleans_canonical_fallback_symlinks(tmp_path, monkeypatch):
    """A worktree containing only the canonical-fallback ``.venv`` symlink
    must be cleanly removed by ``tusk merge``. Without the pre-clean,
    ``git worktree remove`` refuses with "contains modified or untracked
    files" (issues #910/#927).
    """
    repo, db_path, env = _seed_repo(tmp_path, monkeypatch)
    task_id, session_id = _insert_task_and_start_session(
        db_path, "Update README.md and verify"
    )

    workspace_root = tmp_path / "workspaces"
    create = _run(
        [
            "task-worktree", "create",
            str(task_id), "symlinktest",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert create.returncode == 0, (
        f"task-worktree create failed: stdout={create.stdout} stderr={create.stderr}"
    )
    payload = json.loads(create.stdout)
    wt = payload["workspace_path"]

    # The canonical-fallback walker should have created a .venv symlink in
    # the worktree pointing at the primary's .venv. Assert that, then
    # confirm the worktree contains no other dirty state (just the
    # symlink).
    venv_path = os.path.join(wt, ".venv")
    assert os.path.islink(venv_path), (
        f".venv must be auto-symlinked into the worktree; ls {wt}: "
        f"{os.listdir(wt)}"
    )

    # Make one in-cone commit so merge has something to ship.
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    # Stage the change from inside the worktree.
    readme_in_wt = os.path.join(wt, "README.md")
    with open(readme_in_wt, "w", encoding="utf-8") as f:
        f.write("changed from worktree\n")
    # Switch to the worktree's branch in the worktree and commit there.
    commit_result = _run(
        ["commit", str(task_id), "edit", "README.md", "--skip-verify"],
        cwd=wt,
        env=env,
    )
    assert commit_result.returncode == 0, (
        f"tusk commit failed: stdout={commit_result.stdout} "
        f"stderr={commit_result.stderr}"
    )

    # Now run merge from the worktree.
    merge = _run(
        ["merge", str(task_id), "--session", str(session_id)],
        cwd=wt,
        env=env,
    )
    assert merge.returncode == 0, (
        f"tusk merge should succeed when only dirty state is tusk-created "
        f"symlinks; stdout={merge.stdout} stderr={merge.stderr}"
    )

    # The worktree directory should be gone.
    assert not os.path.exists(wt), (
        f"worktree {wt} should be removed after successful merge"
    )


def test_merge_cleans_generated_pytest_cache(tmp_path, monkeypatch):
    """A generated ``.pytest_cache`` must not strand an otherwise-clean
    completed task worktree, even when no auto-symlink cleanup is involved.
    """
    repo, db_path, env = _seed_repo(tmp_path, monkeypatch)
    env["TUSK_NO_AUTO_SYMLINK"] = "1"
    monkeypatch.setenv("TUSK_NO_AUTO_SYMLINK", "1")
    task_id, session_id = _insert_task_and_start_session(
        db_path, "Update README.md"
    )

    workspace_root = tmp_path / "workspaces"
    create = _run(
        [
            "task-worktree", "create",
            str(task_id), "pytestcache",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert create.returncode == 0, create.stderr
    wt = json.loads(create.stdout)["workspace_path"]

    readme_in_wt = os.path.join(wt, "README.md")
    with open(readme_in_wt, "w", encoding="utf-8") as f:
        f.write("changed\n")
    commit_result = _run(
        ["commit", str(task_id), "edit", "README.md", "--skip-verify"],
        cwd=wt,
        env=env,
    )
    assert commit_result.returncode == 0, commit_result.stderr

    cache_file = os.path.join(wt, ".pytest_cache", "v", "cache", "nodeids")
    os.makedirs(os.path.dirname(cache_file))
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write("[]\n")

    merge = _run(
        ["merge", str(task_id), "--session", str(session_id)],
        cwd=wt,
        env=env,
    )
    assert merge.returncode == 0, (
        f"generated pytest cache should not block cleanup; "
        f"stdout={merge.stdout} stderr={merge.stderr}"
    )
    assert not os.path.exists(wt), (
        f"worktree {wt} should be removed after cache cleanup"
    )


def test_merge_preserves_unrelated_symlinks(tmp_path, monkeypatch):
    """The pre-clean must NOT remove symlinks whose basename is outside the
    ``worktree.symlink_files`` + canonical fallback set. A user-created
    symlink (e.g. ``my-link`` → ``/tmp/foo``) must survive the cleanup
    so its presence still blocks ``git worktree remove`` as expected.
    """
    repo, db_path, env = _seed_repo(tmp_path, monkeypatch)
    task_id, session_id = _insert_task_and_start_session(
        db_path, "Update README.md"
    )

    workspace_root = tmp_path / "workspaces"
    create = _run(
        [
            "task-worktree", "create",
            str(task_id), "unrelated",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert create.returncode == 0
    payload = json.loads(create.stdout)
    wt = payload["workspace_path"]

    # Add an unrelated user-created symlink.
    target = tmp_path / "external-target"
    target.write_text("x\n", encoding="utf-8")
    os.symlink(str(target), os.path.join(wt, "my-custom-link"))

    # Make a commit so merge has something to land.
    readme_in_wt = os.path.join(wt, "README.md")
    with open(readme_in_wt, "w", encoding="utf-8") as f:
        f.write("changed\n")
    commit_result = _run(
        ["commit", str(task_id), "edit", "README.md", "--skip-verify"],
        cwd=wt,
        env=env,
    )
    assert commit_result.returncode == 0, commit_result.stderr

    merge = _run(
        ["merge", str(task_id), "--session", str(session_id)],
        cwd=wt,
        env=env,
    )
    # TASK-504: tusk merge now exits 3 (not 0) when post-merge worktree
    # cleanup fails, so automation can detect the leftover worktree /
    # branch without grepping stderr. The task is still Done and the
    # branch is pushed — only the local cleanup needs manual attention.
    assert merge.returncode == 3, (
        f"merge must exit 3 (cleanup-only failure) when an unrelated "
        f"symlink blocks `git worktree remove`; got exit "
        f"{merge.returncode}\nstdout={merge.stdout}\nstderr={merge.stderr}"
    )
    assert "contains modified or untracked files" in merge.stderr or (
        "git worktree remove" in merge.stderr and "failed" in merge.stderr
    ), (
        f"merge stderr should surface the worktree-remove failure when an "
        f"unrelated symlink remains; stderr was: {merge.stderr}"
    )
    # The unrelated symlink must still exist on disk.
    assert os.path.islink(os.path.join(wt, "my-custom-link")), (
        "unrelated symlink must NOT be removed by the cleanup"
    )
    # The worktree directory must still exist (the remove failed).
    assert os.path.exists(wt), (
        "worktree should still exist when remove failed on the unrelated "
        "symlink"
    )
