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
import shutil
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


def test_default_cone_unions_pytest_source_helpers(tmp_path, monkeypatch):
    """Configured pytest targets also pull source-repo helper modules.

    The test command only names ``tests/unit/``, but tusk's source-repo tests
    import ``bin/tusk-*.py`` by path. A docs/skill-scoped task must therefore
    materialize ``bin`` even when its own scope and sparse defaults do not.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    config_path = repo / "tusk" / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["test_command"] = "python3 -m pytest tests/unit/ -q"
    scope_cfg = cfg.setdefault("scope", {})
    scope_cfg["sparse_always_include"] = []
    scope_cfg["sparse_always_cone"] = []
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    (repo / "bin" / "tusk-task-summary.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "seed tusk helper"], cwd=repo)

    task = _insert_task(db_path, "Update docs/notes.md and nothing else")
    workspace_root = tmp_path / "workspaces"

    result = _run(
        [
            "task-worktree",
            "create",
            str(task),
            "pytesthelpers",
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
    assert "bin" in cone, (
        f"pytest tests should pull source helpers into the cone; got {cone}"
    )
    assert os.path.isfile(
        os.path.join(payload["workspace_path"], "bin", "tusk-task-summary.py")
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
    assert "scope.sparse_always_cone" in result.stdout, (
        "scope.sparse_always_cone missing from --cone help text; stdout was: "
        f"{result.stdout}"
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


# ── Rule 19 sparse gate (issue #922) ────────────────────────────────


def test_rule19_skipped_under_sparse_checkout(tmp_path, monkeypatch):
    """Rule 19 returns [] when sparse-checkout is active.

    Without the gate, Rule 19 reports `.claude/tusk-manifest.json file not
    found` whenever the cone excludes `.claude/`, causing every commit to
    fail with blocking exit 6 (issue #922). The gate mirrors Rule 18's
    sparse short-circuit at bin/tusk-lint.py:1072.
    """
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    # Drop the .claude/tusk-manifest.json so Rule 19 would otherwise report
    # 'file not found'; the sparse gate must suppress this.
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)

    lint = _load_rule18(monkeypatch)
    violations = lint.rule19_tusk_manifest_json_sync(str(repo))
    assert violations == [], (
        f"Rule 19 should return [] under sparse-checkout; got {violations}"
    )


def test_rule19_still_fires_without_sparse_checkout(tmp_path, monkeypatch):
    """Rule 19 still reports drift when sparse-checkout is NOT active.

    Pairs with the rule18 non-sparse test — confirms the gate is strictly
    gated on the sparse signal and normal drift detection survives.
    """
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    # No sparse-checkout. .claude/tusk-manifest.json is missing → "file not
    # found" violation should fire.
    lint = _load_rule18(monkeypatch)
    violations = lint.rule19_tusk_manifest_json_sync(str(repo))
    assert violations, (
        "Rule 19 should fire when sparse-checkout is off and "
        ".claude/tusk-manifest.json is missing"
    )
    assert any("tusk-manifest.json" in v for v in violations), (
        f"violations should mention tusk-manifest.json; got {violations}"
    )


def test_rule12_py_compile_does_not_write_repo_pycache(tmp_path, monkeypatch):
    """Rule 12 syntax-checks bin helpers without writing ``bin/__pycache__``."""
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    (repo / "bin" / "tusk-valid.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8"
    )
    pycache = repo / "bin" / "__pycache__"
    assert not pycache.exists(), "fixture should start without repo bytecode"

    lint = _load_rule18(monkeypatch)
    violations = lint.rule12_python_syntax(str(repo))

    assert violations == []
    assert not pycache.exists(), "Rule 12 should not write bytecode into bin/"


# ── sparse_always_cone config key (issue #935) ──────────────────────


def test_sparse_always_cone_widens_cone(tmp_path, monkeypatch):
    """``scope.sparse_always_cone`` entries land in the cone verbatim
    (no dirname extraction), so a source-repo config can force `.claude/`,
    `skills/`, `.github/`, etc. into every task worktree without needing
    a placeholder file path (issue #935).
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)

    # Add the new key to tusk/config.json's scope block.
    config_path = repo / "tusk" / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    scope_cfg = cfg.setdefault("scope", {})
    scope_cfg["sparse_always_cone"] = [".claude", "skills", ".github"]
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # Seed directory entries so cone-mode set has something concrete to
    # materialize, then commit so they're present in HEAD.
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "marker.txt").write_text("c\n", encoding="utf-8")
    (repo / "skills").mkdir(exist_ok=True)
    (repo / "skills" / "marker.txt").write_text("s\n", encoding="utf-8")
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ".github" / "marker.txt").write_text("g\n", encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "seed always_cone dirs"], cwd=repo)

    # Insert a task that references at least one path (so sparse-checkout
    # is applied at all) but doesn't reference the always_cone dirs.
    import sqlite3 as _sqlite3

    with _sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "('cone test', ?, 'To Do', 'feature', 'High', 'M', 30)",
            ("Update bin/marker.txt and verify",),
        )
        conn.commit()
        task_id = cur.lastrowid

    (repo / "bin" / "marker.txt").write_text("b\n", encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "bin marker"], cwd=repo)

    workspace_root = tmp_path / "workspaces"
    result = subprocess.run(
        [
            TUSK_BIN, "task-worktree", "create",
            str(task_id), "conetest",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"task-worktree create failed:\nSTDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    wt = payload["workspace_path"]

    cone_result = subprocess.run(
        ["git", "-C", wt, "sparse-checkout", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    cone_entries = set(
        line.strip() for line in cone_result.stdout.splitlines() if line.strip()
    )

    # All three always_cone entries must be in the cone verbatim.
    for d in (".claude", "skills", ".github"):
        assert d in cone_entries, (
            f"sparse_always_cone entry {d!r} should land in cone; got {cone_entries}"
        )

    # And the directories must be materialized in the worktree.
    for d in (".claude", "skills", ".github"):
        assert os.path.isdir(os.path.join(wt, d)), (
            f"{d}/ should be materialized; ls {wt}: {os.listdir(wt)}"
        )
        assert os.path.isfile(os.path.join(wt, d, "marker.txt")), (
            f"{d}/marker.txt should be materialized"
        )


def test_sparse_always_cone_normalizes_unsafe_entries(tmp_path, monkeypatch):
    """``sparse_always_cone`` entries with ``..`` or absolute paths are
    filtered out by ``_normalize_cone_entry`` before reaching git, so a
    malformed config can't trigger the issue #928 normalize failure
    through this new code path.
    """
    repo, db_path, env = _repo_with_tusk(tmp_path, monkeypatch)

    config_path = repo / "tusk" / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    scope_cfg = cfg.setdefault("scope", {})
    scope_cfg["sparse_always_cone"] = ["../bad", "/abs/path", ".claude"]
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "marker.txt").write_text("c\n", encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "seed .claude"], cwd=repo)

    import sqlite3 as _sqlite3

    with _sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, "
            "priority, complexity, priority_score) VALUES "
            "('cone test 2', ?, 'To Do', 'feature', 'High', 'M', 30)",
            ("Update bin/marker.txt",),
        )
        conn.commit()
        task_id = cur.lastrowid

    (repo / "bin" / "marker.txt").write_text("b\n", encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "bin marker"], cwd=repo)

    workspace_root = tmp_path / "workspaces"
    result = subprocess.run(
        [
            TUSK_BIN, "task-worktree", "create",
            str(task_id), "conesanitize",
            "--workspace-root", str(workspace_root),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    wt = payload["workspace_path"]

    cone_result = subprocess.run(
        ["git", "-C", wt, "sparse-checkout", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    cone_entries = set(
        line.strip() for line in cone_result.stdout.splitlines() if line.strip()
    )

    # Bad entries filtered, safe entry survived.
    for bad in ("../bad", "/abs/path", "abs/path"):
        assert bad not in cone_entries, (
            f"unsafe entry {bad!r} should be filtered; got {cone_entries}"
        )
    assert ".claude" in cone_entries, (
        f"safe entry .claude should survive; got {cone_entries}"
    )
    # No falls-back-to-full-checkout advisory: filtering happened before the
    # set call so init/set both succeeded.
    assert "falls back to a full checkout" not in result.stderr, (
        f"filtering should prevent the failure; stderr was: {result.stderr}"
    )


# ── Criterion 2228 (issues #895 / #905) ─────────────────────────────


def _install_generate_manifest(repo):
    """Copy the real generate-manifest script (and its only import dependency)
    into ``repo``'s bin/ so it operates on the tmp repo under test.

    generate-manifest's get_repo_root() resolves its target repo from the
    script's own location (__file__), NOT the cwd it is run in (issue #882
    deliberately made this change to stop sibling-repo misresolution). So
    invoking the SUITE checkout's binary against a tmp repo enumerates the
    suite checkout and ignores the tmp repo entirely — which made these
    tests pass-by-accident from the primary full checkout and false-fail
    from a sparse task worktree (issue #1094). Copying the script into the
    tmp repo's bin/ makes __file__ resolve to the tmp repo so the tmp repo's
    own sparse state drives the behavior, from any invoking checkout.
    """
    dest_bin = repo / "bin"
    for name in ("tusk-generate-manifest.py", "tusk_underscore_bin_files.py"):
        shutil.copy(
            os.path.join(REPO_ROOT, "bin", name),
            os.path.join(str(dest_bin), name),
        )


def _run_generate_manifest(repo, env):
    """Invoke the tmp repo's own generate-manifest script directly so its
    __file__-derived repo root is the tmp repo (see _install_generate_manifest)."""
    return subprocess.run(
        ["python3", os.path.join(str(repo / "bin"), "tusk-generate-manifest.py")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_generate_manifest_refuses_under_sparse(tmp_path, monkeypatch):
    """``tusk generate-manifest`` exits non-zero and leaves MANIFEST untouched
    when invoked under a sparse-checkout worktree.

    The before-fix behavior silently regenerated MANIFEST from the sparse
    view, dropping entries for every unmaterialized file (issues #895/#905).
    """
    repo, _db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    _install_generate_manifest(repo)
    manifest_before = (repo / "MANIFEST").read_text(encoding="utf-8")
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)

    result = _run_generate_manifest(repo, env)
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
    _install_generate_manifest(repo)
    # No sparse-checkout.
    result = _run_generate_manifest(repo, env)
    assert result.returncode == 0, (
        f"generate-manifest should succeed in a full checkout;\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def _load_generate_manifest(monkeypatch):
    """Import build_manifest from ``bin/tusk-generate-manifest.py`` by path."""
    import importlib.util

    bin_dir = os.path.join(REPO_ROOT, "bin")
    monkeypatch.syspath_prepend(bin_dir)
    spec = importlib.util.spec_from_file_location(
        "_tusk_generate_manifest_for_test",
        os.path.join(bin_dir, "tusk-generate-manifest.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_manifest_refuses_under_sparse_via_import(tmp_path, monkeypatch):
    """build_manifest() refuses under sparse-checkout when called directly via
    Python import — defense-in-depth against the issue #909 bypass class.

    Before the fix, the sparse-checkout gate lived only in main(); any caller
    that imported build_manifest directly walked the on-disk source tree
    unprotected and returned a partial entry list, which a caller that then
    wrote the result to MANIFEST would silently corrupt (issues #895 / #905).
    """
    import pytest

    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    _git(["sparse-checkout", "init", "--cone"], cwd=repo)
    _git(["sparse-checkout", "set", "bin"], cwd=repo)

    gen = _load_generate_manifest(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        gen.build_manifest(str(repo))
    assert excinfo.value.code == 1


def test_build_manifest_runs_in_full_checkout_via_import(tmp_path, monkeypatch):
    """build_manifest() returns its enumeration in a non-sparse worktree when
    called via Python import — confirms the import-path gate is signal-gated
    on sparse-checkout, not always-on."""
    repo, _db_path, _env = _repo_with_tusk(tmp_path, monkeypatch)
    _seed_source_repo_layout(repo)
    # No sparse-checkout.
    gen = _load_generate_manifest(monkeypatch)
    entries = gen.build_manifest(str(repo))
    assert isinstance(entries, list) and entries, (
        "build_manifest should return a non-empty list in a full checkout"
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


# ── Criterion 2232 (issue #893) ─────────────────────────────────────


def test_migrate_dispatches_to_worktree_binary(tmp_path, monkeypatch):
    """When invoked from a worktree CWD whose ``bin/tusk-migrate.py`` differs
    from the invoking dispatcher's ``$SCRIPT_DIR/tusk-migrate.py``, ``tusk
    migrate`` re-execs into the worktree binary. Without this, a worktree-
    local new migration silently no-ops against the primary's older binary
    (issue #893, original incident TASK-471).
    """
    # Build a separate git repo that pretends to be a tusk worktree: it has
    # its own bin/tusk-migrate.py that prints a sentinel and exits 0, so we
    # can detect whether that script ran (vs the primary's real migrate).
    worktree = tmp_path / "worktree-repo"
    worktree.mkdir()
    _git(["init", "-b", "main"], cwd=worktree)
    _git(["config", "user.email", "tusk@example.test"], cwd=worktree)
    _git(["config", "user.name", "Tusk Tests"], cwd=worktree)
    (worktree / "bin").mkdir()
    (worktree / "bin" / "tusk-migrate.py").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('TUSK_MIGRATE_WORKTREE_SENTINEL', file=sys.stderr)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    (worktree / "README.md").write_text("worktree\n", encoding="utf-8")
    _git(["add", "."], cwd=worktree)
    _git(["commit", "-m", "init"], cwd=worktree)

    # Pin a DB path so the primary's tusk dispatcher has a real DB to point
    # tusk-migrate.py at — the sentinel script ignores it but the dispatcher
    # still needs DB_PATH to be resolvable.
    db_path = tmp_path / "tasks.db"
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    env["TUSK_QUIET"] = "1"
    # tusk init pinned to a known path (it doesn't need to be in the worktree).
    init_repo = tmp_path / "init-repo"
    init_repo.mkdir()
    _git(["init", "-b", "main"], cwd=init_repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=init_repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=init_repo)
    (init_repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(["add", "."], cwd=init_repo)
    _git(["commit", "-m", "initial"], cwd=init_repo)
    result = _run(["init", "--force", "--skip-gitignore"], cwd=init_repo, env=env)
    assert result.returncode == 0, result.stderr

    # Now invoke the primary's tusk migrate from the worktree CWD. The
    # dispatcher should detect the worktree's bin/tusk-migrate.py and re-exec
    # into it instead of the primary's tusk-migrate.py.
    result = _run(["migrate"], cwd=worktree, env=env)
    combined = result.stdout + result.stderr
    assert "TUSK_MIGRATE_WORKTREE_SENTINEL" in combined, (
        f"primary tusk migrate should have dispatched to worktree's "
        f"bin/tusk-migrate.py; combined output was:\n{combined}"
    )


def test_migrate_legacy_path_when_no_worktree_binary(tmp_path, monkeypatch):
    """When the CWD's REPO_ROOT has no ``bin/tusk-migrate.py``, ``tusk
    migrate`` uses the primary's binary as before — confirms the dispatch
    preference is gated strictly on the worktree-binary presence."""
    repo, _db_path, env = _repo_with_tusk(tmp_path, monkeypatch)
    # _repo_with_tusk's repo has bin/some-script but NOT bin/tusk-migrate.py,
    # so the legacy path applies.
    assert not (repo / "bin" / "tusk-migrate.py").exists(), (
        "fixture invariant: bin/tusk-migrate.py should not exist in this repo"
    )
    result = _run(["migrate"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"legacy migrate path should succeed;\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    # The dispatch advisory must NOT fire when worktree binary is absent.
    assert "dispatching to worktree migrate binary" not in result.stderr, (
        f"legacy path should not announce worktree dispatch; stderr was:\n{result.stderr}"
    )


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
