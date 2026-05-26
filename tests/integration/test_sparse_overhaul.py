"""Integration tests for the TASK-480 sparse-checkout overhaul.

Covers the six surfaces consolidated under TASK-480:

  - Criterion 2230 (issue #892) — task-worktree create's default cone unions
    the configured ``test_command``'s target paths so the first ``tusk commit``
    does not hard-fail on "file or directory not found: tests/unit/".
  - Criterion 2231 (issue #896) — task-worktree create accepts repeatable
    ``--cone <path>`` to pre-declare extra sparse paths.
  - Criterion 2227 (issue #904) — Rule 18 (MANIFEST drift) detects sparse-
    checkout state and skips the drift check instead of reporting every
    out-of-cone file as missing-from-source-tree.
  - Criterion 2228 (issues #895 / #905) — ``tusk generate-manifest`` refuses
    to overwrite MANIFEST when invoked under a sparse worktree, to prevent
    silent destruction of the entries for unmaterialized files.
  - Criterion 2229 (issue #906) — ``tusk commit`` info-skips the test_command
    gate when sparse-checkout is active and the test command's target path
    is outside the cone, instead of hard-failing with exit 2.
  - Criterion 2232 (issue #893) — ``tusk migrate`` invoked from a worktree
    CWD whose ``bin/tusk-migrate.py`` differs from the dispatcher's
    ``$SCRIPT_DIR/tusk-migrate.py`` re-execs into the worktree binary.
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


def _git(args, *, cwd, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check:
        assert result.returncode == 0, (
            f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
    return result


def _repo_with_tusk(tmp_path, monkeypatch):
    """Build a test repo whose layout exercises the sparse-checkout cone sources.

    Top-level always_allowed (VERSION, CHANGELOG.md, MANIFEST), nested always_allowed
    (.claude/tusk-manifest.json), sparse_always_include defaults (bin/, tests/),
    a task-referenced area (tests/integration/), and out-of-cone regions
    (tests/unit/, docs/, skills/, hooks/) so we can assert both inclusion and
    exclusion concretely.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("test repo\n", encoding="utf-8")
    (repo / "VERSION").write_text("1\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (repo / "MANIFEST").write_text("[]\n", encoding="utf-8")
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
    (repo / "skills" / "tusk").mkdir(parents=True)
    (repo / "skills" / "tusk" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "noop.sh").write_text("#!/bin/sh\n", encoding="utf-8")
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
            "VALUES ('sparse overhaul test', ?, 'To Do', 'feature', 'High', 'M', 30)",
            (description,),
        )
        conn.commit()
        return cur.lastrowid


def _sparse_cone(worktree):
    """Return cone entries set on ``worktree``, or None if sparse-checkout is off."""
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


# ── Criterion 2230 (issue #892) ─────────────────────────────────────


def test_default_cone_unions_test_command_paths(tmp_path, monkeypatch):
    """The configured test_command's target paths are unioned into the cone.

    Without this fix, a task that references only files under ``bin/`` produces
    a cone of ``bin``, the default test_command ``python3 -m pytest tests/unit/``
    fails with "file or directory not found: tests/unit/" on the first commit,
    and every commit in the session has to use ``--skip-verify``.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    # Pin a test_command that exercises a directory outside any other cone source.
    config_path = repo / "tusk" / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["test_command"] = "python3 -m pytest tests/unit/ -q"
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    task = _insert_task(db_path, "Touch bin/something-only.py and nothing else")
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "tcp",
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
    assert "tests" in cone, (
        f"test_command's tests/unit/ should pull tests into the cone; got {cone}"
    )
    # And the file should materialize.
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], "tests", "unit", "test_b.py")
    )


# ── Criterion 2231 (issue #896) ─────────────────────────────────────


def test_cone_flag_extends_cone(tmp_path, monkeypatch):
    """``--cone PATH`` repeatable arg pre-declares extra sparse paths."""
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    task = _insert_task(db_path, "Edit bin/tusk-foo.py")
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "coneflag",
            "--workspace-root",
            str(workspace_root),
            "--cone",
            "docs",
            "--cone",
            "skills",
            "--cone",
            "hooks",
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    cone = _sparse_cone(payload["workspace_path"])
    assert cone is not None, "sparse-checkout should be enabled"
    for path in ("docs", "skills", "hooks"):
        assert path in cone, (
            f"--cone {path} should be in the cone; got {cone}"
        )
    wt = payload["workspace_path"]
    assert os.path.isfile(os.path.join(wt, "docs", "notes.md"))
    assert os.path.isfile(os.path.join(wt, "skills", "tusk", "SKILL.md"))
    assert os.path.isfile(os.path.join(wt, "hooks", "noop.sh"))


def test_cone_flag_help_text(tmp_path, monkeypatch):
    """``--cone`` shows up in --help so operators can discover it."""
    repo, _db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    result = _run(["task-worktree", "create", "--help"], cwd=repo, env=env)
    # argparse prints --help and exits 0; the help text is in stdout.
    assert result.returncode == 0, result.stderr
    assert "--cone" in result.stdout, (
        f"--cone missing from help text; stdout was: {result.stdout}"
    )


# ── Criterion 2227 (issue #904) ─────────────────────────────────────


# ── Criterion 2227 (issue #904) ─────────────────────────────────────


def _load_rule18(monkeypatch):
    """Import ``rule18_manifest_drift`` from ``bin/tusk-lint.py``.

    The file is named with a hyphen so it's not a normal importable module;
    use importlib by path so the test can call the function directly without
    going through the full ``tusk lint`` driver (which would run all 25+
    rules against a minimal test repo and produce noisy unrelated output).
    """
    import importlib.util

    bin_dir = os.path.join(REPO_ROOT, "bin")
    monkeypatch.syspath_prepend(bin_dir)
    spec = importlib.util.spec_from_file_location(
        "_tusk_lint_for_test",
        os.path.join(bin_dir, "tusk-lint.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_source_repo_layout(repo):
    """Stamp the test repo with enough source-repo layout to drive Rule 18.

    Rule 18 is gated on ``bin/tusk`` existing at the repo root (source-repo
    sentinel). We also write a MANIFEST referencing files that don't exist
    on disk so the rule would report drift in a non-sparse worktree — that's
    how we prove the sparse-aware short-circuit is the thing suppressing the
    drift output, not just an empty MANIFEST.
    """
    (repo / "bin" / "tusk").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "bin" / "dist-excluded.txt").write_text("", encoding="utf-8")
    (repo / "MANIFEST").write_text(
        json.dumps([
            ".claude/bin/tusk",
            ".claude/skills/tusk/SKILL.md",
            ".claude/hooks/noop.sh",
        ], indent=2) + "\n",
        encoding="utf-8",
    )


def test_rule18_skipped_under_sparse_checkout(tmp_path, monkeypatch):
    """Rule 18 returns [] when sparse-checkout is active.

    Without the fix, Rule 18 walks the on-disk source tree and reports every
    file present in MANIFEST but absent in the sparse view as "extra in
    MANIFEST but not in source tree" — the issue #904 cluster (17+ false
    positives in a typical sparse worktree, which then trigger the auto-
    `generate-manifest` retry path and silently destroy MANIFEST entries).
    """
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)

    lint = _load_rule18(monkeypatch)
    violations = lint.rule18_manifest_drift(str(repo))
    assert violations == [], (
        f"Rule 18 should return [] under sparse-checkout; got {violations}"
    )


def test_rule18_still_fires_without_sparse_checkout(tmp_path, monkeypatch):
    """Rule 18 still reports drift when sparse-checkout is NOT active.

    Confirms the short-circuit is gated strictly on the sparse-checkout
    signal — normal drift detection survives intact.
    """
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    # No sparse-checkout enabled.
    lint = _load_rule18(monkeypatch)
    violations = lint.rule18_manifest_drift(str(repo))
    assert violations, (
        "Rule 18 should report drift when sparse-checkout is off but MANIFEST "
        "references files absent from the source tree"
    )


# ── Criterion 2228 (issues #895 / #905) ─────────────────────────────


def test_generate_manifest_refuses_under_sparse(tmp_path, monkeypatch):
    """``tusk generate-manifest`` exits non-zero and leaves MANIFEST untouched
    when invoked under a sparse-checkout worktree.

    The before-fix behavior silently regenerated MANIFEST from the sparse
    view, dropping entries for every unmaterialized file (issues #895/#905).
    """
    repo, _db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    manifest_before = (repo / "MANIFEST").read_text(encoding="utf-8")
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)

    result = _run(["generate-manifest"], cwd=repo, env=env)
    assert result.returncode != 0, (
        f"generate-manifest should refuse under sparse-checkout; "
        f"exit={result.returncode}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "sparse" in result.stderr.lower(), (
        f"refusal message should mention sparse-checkout; stderr was:\n{result.stderr}"
    )
    manifest_after = (repo / "MANIFEST").read_text(encoding="utf-8")
    assert manifest_before == manifest_after, (
        "MANIFEST should not have been rewritten when refused"
    )


def test_generate_manifest_runs_in_full_checkout(tmp_path, monkeypatch):
    """In a non-sparse worktree, generate-manifest runs as before — confirms
    the sparse refusal is gated strictly on the sparse-checkout signal."""
    repo, _db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    # No sparse-checkout.
    result = _run(["generate-manifest"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"generate-manifest should succeed in a full checkout;\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


# ── Criterion 2229 (issue #906) ─────────────────────────────────────


def _load_commit_helpers(monkeypatch):
    """Import the sparse-aware helpers from ``bin/tusk-commit.py`` by path."""
    import importlib.util

    bin_dir = os.path.join(REPO_ROOT, "bin")
    monkeypatch.syspath_prepend(bin_dir)
    spec = importlib.util.spec_from_file_location(
        "_tusk_commit_for_test",
        os.path.join(bin_dir, "tusk-commit.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_test_command_outside_sparse_cone_detects_missing_path(
    tmp_path, monkeypatch
):
    """``_test_command_outside_sparse_cone`` returns (True, target) when the
    test command's path token is absent on disk under sparse-checkout."""
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)
    # tests/unit/ exists in the repo's HEAD but the cone is 'bin', so it's not
    # materialized on disk.
    assert not (repo / "tests" / "unit").exists(), (
        "fixture invariant: tests/unit should not be materialized under cone=bin"
    )
    commit = _load_commit_helpers(monkeypatch)
    outside, target = commit._test_command_outside_sparse_cone(
        "python3 -m pytest tests/unit/ -q", str(repo)
    )
    assert outside is True
    assert target == "tests/unit/"


def test_test_command_outside_sparse_cone_present_path(tmp_path, monkeypatch):
    """When the test command's target IS materialized, the helper returns False."""
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    # No sparse-checkout, so tests/unit/ exists on disk.
    commit = _load_commit_helpers(monkeypatch)
    outside, _target = commit._test_command_outside_sparse_cone(
        "python3 -m pytest tests/unit/ -q", str(repo)
    )
    assert outside is False


def test_commit_info_skips_when_test_command_path_outside_cone(
    tmp_path, monkeypatch
):
    """End-to-end: ``tusk commit`` succeeds (info-skip) when sparse-checkout
    excludes the configured test_command's target path. Without the fix,
    every commit in the session would exit 2 forcing --skip-verify everywhere.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    # Pin test_command to a path that will be outside the cone.
    config_path = repo / "tusk" / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["test_command"] = "python3 -m pytest tests/unit/ -q"
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    task = _insert_task(db_path, "Edit bin/some-script for issue X")
    # Build a feature branch manually so we don't depend on task-worktree.
    _git(["checkout", "-b", "feature/TASK-1-test"], cwd=repo)
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)
    # Record the workspace so tusk commit doesn't refuse on branch-mismatch.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task, "feature/TASK-1-test", str(repo)),
        )
        conn.commit()
    # Add a criterion so commit --criteria has something to mark.
    cres = _run(
        ["criteria", "add", str(task), "Stub criterion for sparse skip test"],
        cwd=repo,
        env=env,
    )
    assert cres.returncode == 0, cres.stderr
    cid = json.loads(cres.stdout)["id"]

    # Modify an in-cone file so there's a real change to commit.
    (repo / "bin" / "some-script").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    result = _run(
        [
            "commit",
            str(task),
            "Edit bin/some-script",
            "bin/some-script",
            "--criteria",
            str(cid),
        ],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, (
        f"tusk commit should info-skip the test gate under sparse-cone-miss; "
        f"exit={result.returncode}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "sparse-checkout cone" in combined or "skipping test gate" in combined, (
        f"expected sparse-skip info line; output was:\n{combined}"
    )
