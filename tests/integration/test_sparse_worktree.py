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

import pytest


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
    (repo / ".claude" / "skills").mkdir()
    (repo / "skills" / "tusk").mkdir(parents=True)
    (repo / "skills" / "tusk" / "SKILL.md").write_text(
        "# Tusk skill\n", encoding="utf-8"
    )
    os.symlink("../../skills/tusk", repo / ".claude" / "skills" / "tusk")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "web-ci.yml").write_text(
        "name: Web CI\n", encoding="utf-8"
    )
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


def _set_sparse_always_cone(repo, entries):
    config_path = repo / "tusk" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["scope"]["sparse_always_cone"] = entries
    config_path.write_text(json.dumps(config), encoding="utf-8")


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


def test_source_skill_symlink_cone_stays_sparse(tmp_path, monkeypatch):
    """Exact tracked symlinks are normalized to their parent directory.

    The tusk source repo tracks ``.claude/skills/tusk`` as a symlink while
    also forcing ``.claude`` and ``skills`` into every sparse worktree. Passing
    the symlink itself to ``git sparse-checkout set`` exits 128 and used to
    force a full-checkout fallback.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _set_sparse_always_cone(repo, [".claude", "skills"])
    task = _insert_task(
        db_path,
        "Update .claude/skills/tusk/SKILL.md while preserving sparse checkout",
    )

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "skill-symlink",
            "--workspace-root",
            str(tmp_path / "workspaces"),
        ],
        cwd=repo,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    worktree = payload["workspace_path"]
    cone = _sparse_cone(worktree)
    assert cone is not None, "valid skill symlinks must keep sparse checkout enabled"
    assert ".claude/skills/tusk" not in cone
    assert "falls back to a full checkout" not in result.stderr
    assert os.path.islink(os.path.join(worktree, ".claude", "skills", "tusk"))
    assert os.path.isfile(
        os.path.join(worktree, ".claude", "skills", "tusk", "SKILL.md")
    )


def test_sparse_cone_includes_referenced_dot_directory_path(tmp_path, monkeypatch):
    """A task that names .github/workflows/web-ci.yml gets that cone from
    extraction, even when .github is not in sparse_always_cone."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _set_sparse_always_cone(repo, ["bin"])

    task = _insert_task(
        db_path,
        "provide BUNNYCDN_CDN_HOST to .github/workflows/web-ci.yml",
    )
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "dotdir",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, "sparse-checkout should be enabled"
    assert ".github/workflows" in set(cone)
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], ".github", "workflows", "web-ci.yml")
    )


@pytest.mark.parametrize(
    "description",
    [
        "Update tests/integration/test_a.py and add a GitHub Actions job",
        "Update tests/integration/test_a.py and add a CI workflow",
        "Update tests/integration/test_a.py and add GHA coverage",
        "Update tests/integration/test_a.py and wire workflow_dispatch",
    ],
)
def test_sparse_cone_includes_github_for_ci_workflow_prose(
    tmp_path, monkeypatch, description
):
    """CI workflow prose materializes sibling workflows without explicit paths."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _set_sparse_always_cone(repo, ["bin"])
    task = _insert_task(db_path, description)
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "ci-prose",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, "sparse-checkout should be enabled"
    assert ".github" in set(cone)
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], ".github", "workflows", "web-ci.yml")
    )


def test_sparse_cone_does_not_include_github_for_unrelated_prose(
    tmp_path, monkeypatch
):
    """Unrelated task prose should not widen the sparse cone to .github."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _set_sparse_always_cone(repo, ["bin"])
    task = _insert_task(
        db_path,
        "Update tests/integration/test_a.py and document workflow notes",
    )
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "no-ci-prose",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, "sparse-checkout should be enabled"
    assert ".github" not in set(cone)
    assert not os.path.exists(os.path.join(payload["workspace_path"], ".github"))


def test_scope_add_materializes_path_outside_sparse_cone(tmp_path, monkeypatch):
    """Adding scope inside a sparse worktree should make that path editable."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _set_sparse_always_cone(repo, ["bin"])
    task = _insert_task(
        db_path,
        "Update tests/integration/test_a.py and document workflow notes",
    )
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "scope-add",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    wt = payload["workspace_path"]

    assert ".github" not in set(_sparse_cone(wt) or [])
    target = os.path.join(wt, ".github", "workflows", "web-ci.yml")
    assert not os.path.exists(target)

    added = _run(
        [
            "scope",
            "add",
            str(task),
            ".github/workflows/web-ci.yml",
            "--reason",
            "expanded during implementation",
        ],
        cwd=wt,
        env=env,
    )

    assert added.returncode == 0, added.stderr
    assert os.path.isfile(target)
    assert ".github/workflows" in set(_sparse_cone(wt) or [])


def test_sparse_cone_rejects_prose_identifier_tokens(tmp_path, monkeypatch):
    """A dotted identifier pair in prose must not become a bogus cone entry."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task = _insert_task(
        db_path,
        "Update tests/integration/test_a.py while investigating console.error/console.log",
    )
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "identifier",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, "sparse-checkout should be enabled"
    assert "console.error" not in set(cone), cone


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


def test_cone_normalization_drops_parent_traversal(tmp_path, monkeypatch):
    """Cone entries containing ``..`` segments are filtered before being
    passed to ``git sparse-checkout set`` — the trigger for the
    ``fatal: could not normalize path ..`` failure in issue #928. Tested
    via ``--cone "../bad"`` since that's the most direct entry-point for
    a caller-supplied malformed cone; ``_derive_sparse_cone`` applies the
    same normalization to ``task_referenced_paths`` and config-sourced
    entries through ``_normalize_cone_entry``.
    """
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
            "badcone",
            "--workspace-root",
            str(workspace_root),
            "--cone",
            "../bad",
            "--cone",
            "/abs/path",
            "--cone",
            "tests/unit",
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, "sparse-checkout should be enabled"
    # Bad entries must NOT be in the cone — neither as-spelled nor as the
    # `git sparse-checkout` normalized form (which strips a leading `/`).
    for bad in ("../bad", "/abs/path", "abs/path"):
        assert bad not in cone, (
            f"unsafe cone entry {bad!r} should have been filtered; got {cone}"
        )
    # The safe entry survives.
    assert "tests/unit" in cone or "tests" in cone, (
        f"safe --cone entry tests/unit should have landed in cone {cone}"
    )
    # No "falls back to a full checkout" advisory — the bad entries were
    # filtered upstream so git sparse-checkout set never saw them, so init
    # and set both succeeded.
    assert "falls back to a full checkout" not in result.stderr, (
        "filtering should prevent the failure that triggers the fallback "
        f"advisory; stderr was: {result.stderr}"
    )


def test_sparse_failure_disables_fallback(tmp_path, monkeypatch):
    """When sparse-checkout setup fails AFTER the cone has been filtered
    (simulated here by writing a sparse-checkout config that git rejects),
    ``_apply_sparse_checkout`` must invoke ``git sparse-checkout disable``
    so the printed "falls back to a full checkout" advisory matches
    reality — the regression captured by issue #928 (the original report:
    the worktree was left in sparse mode with no patterns, materializing
    only ~1% of tracked files).

    Reaching the failure path through the normal CLI entry point requires
    bypassing ``_normalize_cone_entry``. We do this by monkey-patching
    ``tusk_loader.load("tusk-task-worktree.py").main`` after calling
    ``_apply_sparse_checkout`` directly with a malformed cone.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task = _insert_task(
        db_path,
        "Update tests/integration/test_a.py and verify behavior",
    )
    workspace_root = tmp_path / "workspaces"

    # First create a normal worktree so we can poke at it directly.
    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "fallback",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    wt = payload["workspace_path"]

    # Now invoke _apply_sparse_checkout directly with a malformed cone
    # that bypasses the upstream normalization. This exercises the
    # disable-fallback branch in isolation.
    import importlib.util

    helpers_path = os.path.join(REPO_ROOT, "bin", "tusk-task-worktree.py")
    spec = importlib.util.spec_from_file_location(
        "tusk_task_worktree", helpers_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Pass `..` directly — git sparse-checkout set rejects it with
    # "fatal: could not normalize path ..".
    applied, disabled_fallback, err = mod._apply_sparse_checkout(wt, [".."])

    assert not applied, "sparse-checkout set should fail for `..`"
    assert disabled_fallback, (
        f"disable fallback should succeed; stderr was: {err}"
    )
    # The original sparse-checkout failure is preserved for the caller's
    # advisory message.
    assert "normalize" in err or "could not" in err.lower(), (
        f"original sparse error should be preserved; got: {err}"
    )

    # After the fallback, the worktree must NOT be in sparse mode — this
    # is the literal failing test from issue #928.
    cfg = subprocess.run(
        ["git", "-C", wt, "config", "--get", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    sparse_cfg = cfg.stdout.strip() if cfg.returncode == 0 else ""
    assert sparse_cfg != "true", (
        f"core.sparseCheckout must be unset/false after disable fallback; "
        f"got {sparse_cfg!r}"
    )
    # And every tracked file should be materialized — proving the printed
    # "falls back to a full checkout" advisory now matches reality.
    assert os.path.isfile(os.path.join(wt, "docs", "notes.md")), (
        "docs/notes.md should be materialized after the disable fallback "
        "since the worktree is no longer sparse"
    )
    assert os.path.isfile(os.path.join(wt, "tests", "unit", "test_b.py")), (
        "tests/unit/test_b.py should be materialized after the disable "
        "fallback"
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
