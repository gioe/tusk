"""Integration tests for sparse-checkout in task worktrees (TASK-470).

`tusk task-worktree create` enables cone-mode sparse-checkout on the new
worktree when the task has at least one referenced path. The cone is the
union of:
  - task_referenced_paths (extracted from summary/description/criteria)
  - scope.sparse_always_include from the project config
  - scope.always_allowed from the project config

Falls back to a full checkout when the task references no paths, and is
disabled entirely by TUSK_NO_SPARSE_WORKTREE=1.
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


def _repo_with_tusk(tmp_path, monkeypatch):
    """Build a test repo seeded with files spanning multiple cone regions.

    The fixture creates files at locations that exercise the four cone
    sources: root-level always_allowed (VERSION, CHANGELOG.md, MANIFEST),
    nested always_allowed (.claude/tusk-manifest.json), sparse_always_include
    defaults (bin/, tests/), the task-referenced area (tests/integration/),
    and out-of-cone regions (tests/unit/, docs/) so exclusion can be
    asserted concretely.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    (repo / "VERSION").write_text("1\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (repo / "MANIFEST").write_text("manifest\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "tusk-manifest.json").write_text("{}\n", encoding="utf-8")
    (repo / "bin").mkdir()
    (repo / "bin" / "some-script").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "tests" / "integration").mkdir(parents=True)
    (repo / "tests" / "integration" / "test_a.py").write_text(
        "# test a\n", encoding="utf-8"
    )
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_b.py").write_text(
        "# test b\n", encoding="utf-8"
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("# notes\n", encoding="utf-8")
    _git(["add", "."], cwd=repo)
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


def _insert_task(db_path, description):
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) "
            "VALUES ('sparse test', ?, 'To Do', 'feature', 'High', 'M', 30)",
            (description,),
        )
        conn.commit()
        return cur.lastrowid


def _sparse_cone(worktree):
    """Return the cone entries set on ``worktree``, or None if sparse-checkout
    is disabled. Reads ``core.sparseCheckout`` first so a worktree with no
    sparse-checkout config is distinguished from one with an empty cone.
    """
    cfg = subprocess.run(
        ["git", "-C", str(worktree), "config", "--get", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if cfg.returncode != 0 or cfg.stdout.strip() != "true":
        return None
    result = subprocess.run(
        ["git", "-C", str(worktree), "sparse-checkout", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def test_sparse_cone_set(tmp_path, monkeypatch):
    """When the task has referenced paths, sparse-checkout is enabled and
    the cone is the union of referenced + sparse_always_include + always_allowed."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task = _insert_task(
        db_path,
        "Update tests/integration/test_a.py and verify behavior",
    )
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "sparse",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, (
        "sparse-checkout should be enabled when the task has referenced "
        f"paths; stderr was: {result.stderr}"
    )
    # Cone-mode `set` deduplicates overlapping entries: `tests/integration`
    # (from the referenced path) is subsumed by `tests` (from the
    # sparse_always_include default tests/conftest.py), so only `tests`
    # remains in the listed cone — but every file under either path is
    # materialized. We assert on the surviving cone entries AND on the
    # materialization of files in both regions.
    expected = {"tests", ".claude", "bin"}
    assert expected.issubset(set(cone)), (
        f"cone missing expected entries; got {cone}, expected superset of {expected}"
    )
    wt = payload["workspace_path"]
    # Referenced-path materialization: tests/integration is reachable.
    assert os.path.isfile(os.path.join(wt, "tests", "integration", "test_a.py"))
    # sparse_always_include default: bin/ is reachable.
    assert os.path.isfile(os.path.join(wt, "bin", "some-script"))
    # Out-of-cone directories must NOT be materialized in the worktree.
    assert not os.path.exists(os.path.join(wt, "docs")), (
        "docs/ is out-of-cone and should not be materialized"
    )


def test_full_checkout_fallback(tmp_path, monkeypatch):
    """When the task references zero paths, sparse-checkout is not enabled
    and the worktree gets a full checkout (the pre-TASK-470 behavior)."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task = _insert_task(
        db_path, "do some unrelated work without naming any files"
    )
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "noscope",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert _sparse_cone(payload["workspace_path"]) is None, (
        "sparse-checkout should be disabled for tasks with zero referenced paths"
    )
    # Confirm everything is materialized — including out-of-cone areas.
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], "docs", "notes.md")
    )
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], "tests", "unit", "test_b.py")
    )


def test_env_var_disables(tmp_path, monkeypatch):
    """TUSK_NO_SPARSE_WORKTREE=1 disables sparse-checkout even when the task
    has referenced paths that would normally trigger it."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task = _insert_task(db_path, "Update tests/integration/test_a.py")
    workspace_root = tmp_path / "workspaces"

    env_disabled = dict(env)
    env_disabled["TUSK_NO_SPARSE_WORKTREE"] = "1"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "envdisabled",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env_disabled,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert _sparse_cone(payload["workspace_path"]) is None, (
        "TUSK_NO_SPARSE_WORKTREE=1 should disable sparse-checkout"
    )
    # Out-of-cone files are materialized because sparse-checkout was skipped.
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], "docs", "notes.md")
    )


def test_always_allowed_in_cone(tmp_path, monkeypatch):
    """always_allowed paths (VERSION, CHANGELOG.md, MANIFEST,
    .claude/tusk-manifest.json) are materialized so commit-time bumps
    work, even when the task scope itself does not reference them."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    # Task references a path outside the always_allowed set so we
    # specifically test that always_allowed is added independent of scope.
    task = _insert_task(db_path, "Update tests/integration/test_a.py")
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "always",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    wt = payload["workspace_path"]
    # Root-level always_allowed: auto-included by cone mode.
    assert os.path.isfile(os.path.join(wt, "VERSION"))
    assert os.path.isfile(os.path.join(wt, "CHANGELOG.md"))
    assert os.path.isfile(os.path.join(wt, "MANIFEST"))
    # Nested always_allowed: materialized iff ``.claude`` is in the cone.
    assert os.path.isfile(os.path.join(wt, ".claude", "tusk-manifest.json"))
    cone = _sparse_cone(wt)
    assert cone is not None
    assert ".claude" in cone, (
        f"cone must include .claude so .claude/tusk-manifest.json is "
        f"materialized; got cone={cone}"
    )
