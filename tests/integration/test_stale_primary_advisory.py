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


def _seed_repo_ahead(tmp_path):
    """Build primary + origin where primary is 1 commit ahead of origin."""
    primary = _seed_repo_with_origin(tmp_path, advance_origin=False)
    (primary / "local.txt").write_text("local\n", encoding="utf-8")
    _git(["add", "."], cwd=primary)
    _git(["commit", "-m", "unpushed local commit"], cwd=primary)
    return primary


def _seed_repo_diverged(tmp_path):
    """Build primary + origin where primary is 1 ahead AND 1 behind origin.

    origin advances 1 commit through a second clone, then primary makes its own
    unpushed local commit — leaving the two branches diverged (issue #949).
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
    _git(["remote", "set-head", "origin", "main"], cwd=primary)

    # origin advances by 1 commit via a second clone.
    advancer = tmp_path / "advancer"
    _git(["clone", str(origin), str(advancer)], cwd=tmp_path)
    _git(["config", "user.email", "tusk@example.test"], cwd=advancer)
    _git(["config", "user.name", "Tusk Tests"], cwd=advancer)
    (advancer / "advance.txt").write_text("y\n", encoding="utf-8")
    _git(["add", "."], cwd=advancer)
    _git(["commit", "-m", "advance origin"], cwd=advancer)
    _git(["push", "origin", "main"], cwd=advancer)

    # primary makes its own unpushed local commit — now 1 ahead + 1 behind.
    (primary / "local.txt").write_text("z\n", encoding="utf-8")
    _git(["add", "."], cwd=primary)
    _git(["commit", "-m", "unpushed local commit"], cwd=primary)

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


def test_stale_primary_refuses_before_creating_worktree(tmp_path, monkeypatch):
    """When primary is 1 commit behind origin/main, task-worktree create
    must refuse before creating the task branch or workspace.
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
    assert result.returncode == 2, result.stderr
    assert "behind origin/main" in result.stderr.lower(), (
        f"stderr should contain behind-origin refusal; got: {result.stderr}"
    )
    assert "tusk sync-main" in result.stderr, (
        f"stderr should name the recovery command; got: {result.stderr}"
    )
    assert "--force-stale" in result.stderr, (
        f"stderr should name the explicit bypass; got: {result.stderr}"
    )
    branch = f"feature/TASK-{task_id}-stale-primary-test"
    branch_result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=primary,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert branch_result.returncode != 0, (
        f"stale-primary refusal must not create branch {branch}"
    )
    assert not (workspace_root / "primary" / f"TASK-{task_id}-stale-primary-test").exists()


def test_force_stale_primary_keeps_advisory_and_creates_worktree(tmp_path, monkeypatch):
    """The explicit --force-stale bypass keeps the old advisory behavior for
    operators who intentionally need to create a worktree from stale primary.
    """
    primary = _seed_repo_with_origin(tmp_path, advance_origin=True)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "force-stale-test",
            "--workspace-root", str(workspace_root),
            "--force-stale",
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "behind origin" in result.stderr.lower(), (
        f"force path should retain the stale-primary advisory; got: {result.stderr}"
    )
    assert "tusk sync-main" in result.stderr, (
        f"stderr should name the recovery command; got: {result.stderr}"
    )
    # A non-heavily-dirty primary (only the untracked tusk/ dir) must NOT carry
    # the heavy-dirty stash-round-trip warning (issue #1095).
    assert "uncommitted/untracked" not in result.stderr, (
        f"clean-ish primary should not warn about a dirty stash round-trip; "
        f"got: {result.stderr}"
    )


def test_behind_and_heavily_dirty_primary_warns_about_stash_round_trip(
    tmp_path, monkeypatch
):
    """When primary is behind origin AND heavily dirty, the behind-origin
    advisory appends a warning that the recommended sync-main will stash and
    pop a large surface across the fast-forward — exactly when a stash-pop
    conflict is most likely (issue #1095)."""
    primary = _seed_repo_with_origin(tmp_path, advance_origin=True)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    # Make primary heavily dirty: well above the _HEAVY_DIRTY_THRESHOLD of 10.
    for i in range(15):
        (primary / f"scratch_{i:02d}.txt").write_text("dirty\n", encoding="utf-8")

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "dirty-stale-test",
            "--workspace-root", str(workspace_root),
            "--force-stale",
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "behind origin" in result.stderr.lower(), result.stderr
    assert "uncommitted/untracked file(s)" in result.stderr, (
        f"heavily-dirty behind primary should warn about the stash round-trip; "
        f"got: {result.stderr}"
    )
    assert "stash-pop conflict" in result.stderr, result.stderr


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
    assert "ahead of origin" not in result.stderr.lower(), (
        f"no advisory should fire when primary is up-to-date; got: "
        f"{result.stderr}"
    )


def test_ahead_primary_warns_before_task_work_begins(tmp_path, monkeypatch):
    """When primary has unpushed commits, task-worktree create warns before
    work starts so the later no-checkout merge does not fail at finalization.
    """
    primary = _seed_repo_ahead(tmp_path)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "ahead-primary-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "ahead of origin/main" in stderr.lower(), (
        f"stderr should contain ahead-of-origin advisory; got: {stderr}"
    )
    assert "1 commit(s) ahead" in stderr, (
        f"stderr should include the ahead count; got: {stderr}"
    )
    assert "Push or discard the unpushed commit(s)" in stderr, (
        f"stderr should include publish-or-discard guidance; got: {stderr}"
    )
    assert "later tusk merge may refuse" in stderr, (
        f"stderr should name the merge-time failure mode; got: {stderr}"
    )


def test_env_var_suppresses_advisory(tmp_path, monkeypatch):
    """TUSK_NO_STALE_PRIMARY_ADVISORY=1 silences only the post-create
    advisory on explicitly forced stale worktree creation.
    """
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
            "--force-stale",
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


def test_diverged_primary_reports_ahead_behind_and_recommends_pull_rebase(
    tmp_path, monkeypatch
):
    """When primary is BOTH ahead and behind origin/main (diverged), the
    advisory must report explicit ahead/behind counts, label the state as
    diverged rather than simply behind, and recommend ``git pull --rebase``
    instead of ``tusk sync-main`` (whose ff-only step cannot reconcile a
    divergence). This is the issue #949 Fix 2 acceptance test.
    """
    primary = _seed_repo_diverged(tmp_path)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "diverged-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 2, result.stderr
    stderr = result.stderr
    # Labeled as a divergence, not a plain "behind".
    assert "diverged" in stderr.lower(), (
        f"diverged primary must be labeled as diverged; got: {stderr}"
    )
    # Explicit ahead AND behind counts (1 each in this fixture).
    assert "1 commit(s) ahead" in stderr, f"missing ahead count; got: {stderr}"
    assert "1 behind" in stderr, f"missing behind count; got: {stderr}"
    # Recommends the rebase pull, which is what actually reconciles divergence.
    assert "git pull --rebase origin main" in stderr, (
        f"diverged advisory must recommend git pull --rebase; got: {stderr}"
    )
    branch = f"feature/TASK-{task_id}-diverged-test"
    branch_result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=primary,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert branch_result.returncode != 0, (
        f"diverged-primary refusal must not create branch {branch}"
    )


def test_diverged_primary_does_not_recommend_sync_main_as_the_fix(
    tmp_path, monkeypatch
):
    """The diverged advisory may *mention* sync-main to explain why it won't
    work, but the actionable recovery must be ``git pull --rebase`` — never a
    bare ``Run "tusk sync-main"`` instruction like the pure-behind path emits.
    """
    primary = _seed_repo_diverged(tmp_path)
    db_path, env = _init_tusk(primary, monkeypatch)
    task_id = _insert_task(db_path)

    workspace_root = tmp_path / "workspaces"
    result = _run(
        [
            "task-worktree", "create",
            str(task_id), "diverged-norecommend-test",
            "--workspace-root", str(workspace_root),
        ],
        cwd=primary,
        env=env,
    )
    assert result.returncode == 2, result.stderr
    stderr = result.stderr
    # The pure-behind path's literal recommendation must not appear here.
    assert 'Run "tusk sync-main" in' not in stderr, (
        f"diverged advisory must not recommend running sync-main as the fix; "
        f"got: {stderr}"
    )
    assert "cannot recover a diverged branch" in stderr, (
        f"diverged advisory should explain why sync-main is wrong here; "
        f"got: {stderr}"
    )
