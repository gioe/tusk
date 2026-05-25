"""Integration tests for tusk glossary export worktree resolution (issue #875).

Regression: before the fix, `cmd_export` resolved `<repo_root>` from the shared
DB's location, so `tusk glossary export` from a linked worktree always wrote
`docs/GLOSSARY.md` to the primary checkout. The regenerated markdown then
never reached the feature branch unless the operator copied it across by hand.
The fix calls `git rev-parse --show-toplevel` from CWD so the worktree's
checkout owns the output path, mirroring (in spirit) the worktree-aware
resolution `tusk version-bump` / `tusk changelog-add` already do at the
dispatcher layer.

Cases covered:
- Export from a linked worktree writes the worktree's docs/GLOSSARY.md; the
  primary's docs/GLOSSARY.md is untouched. Stderr names the worktree path.
- Export from a primary checkout still writes the primary's docs/GLOSSARY.md
  (current behavior preserved).
- When CWD is outside any git repo, falls back to the db_path-derived
  repo_root (the pre-fix path).
"""

import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


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


def _tusk(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _seed_tusk_repo(tmp_path):
    """Init a real tusk repo at tmp_path/repo with the production DB layout.

    The DB lives at <repo>/tusk/tasks.db so dirname(dirname(db_path)) == repo
    — matches the pre-fix fallback resolution that the worktree-aware path
    overrides.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tusk").mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    env = os.environ.copy()
    env["TUSK_DB"] = str(repo / "tusk" / "tasks.db")
    env["TUSK_QUIET"] = "1"
    init = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert init.returncode == 0, (
        f"tusk init failed\nSTDOUT: {init.stdout}\nSTDERR: {init.stderr}"
    )
    return repo, env


def _add_glossary_entry(repo, env, term, definition):
    result = _tusk(
        ["glossary", "add", term, definition, "--topics", "test"],
        cwd=repo,
        env=env,
    )
    assert result.returncode == 0, (
        f"glossary add failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_export_from_worktree_writes_worktree_glossary_md(tmp_path):
    """The issue #875 regression: export from a linked worktree must land in the
    worktree's docs/GLOSSARY.md, not the primary's.
    """
    repo, env = _seed_tusk_repo(tmp_path)
    _add_glossary_entry(repo, env, "wt-test-term", "Worktree-only export marker.")

    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "feature/test-export"], cwd=repo)

    # Pre-state: neither checkout has docs/GLOSSARY.md yet.
    assert not (repo / "docs" / "GLOSSARY.md").exists()
    assert not (worktree / "docs" / "GLOSSARY.md").exists()

    result = _tusk(["glossary", "export"], cwd=worktree, env=env)
    assert result.returncode == 0, (
        f"export failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    worktree_path = worktree / "docs" / "GLOSSARY.md"
    primary_path = repo / "docs" / "GLOSSARY.md"
    assert worktree_path.exists(), "expected worktree's docs/GLOSSARY.md to be created"
    assert not primary_path.exists(), (
        f"primary's docs/GLOSSARY.md should NOT have been written; exists at {primary_path}"
    )
    contents = worktree_path.read_text(encoding="utf-8")
    assert "wt-test-term" in contents, (
        f"expected entry to land in worktree's file; got: {contents[:200]!r}"
    )

    advisory = f"Wrote: {worktree_path}"
    assert advisory in result.stderr, (
        f"expected stderr advisory naming the worktree path; got: {result.stderr!r}"
    )


def test_export_from_primary_writes_primary_glossary_md(tmp_path):
    """Behavior on the primary checkout is preserved: export writes to primary."""
    repo, env = _seed_tusk_repo(tmp_path)
    _add_glossary_entry(repo, env, "primary-test-term", "Primary export marker.")

    assert not (repo / "docs" / "GLOSSARY.md").exists()

    result = _tusk(["glossary", "export"], cwd=repo, env=env)
    assert result.returncode == 0, (
        f"export failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    primary_path = repo / "docs" / "GLOSSARY.md"
    assert primary_path.exists(), "expected primary's docs/GLOSSARY.md to be created"
    assert "primary-test-term" in primary_path.read_text(encoding="utf-8")

    advisory = f"Wrote: {primary_path}"
    assert advisory in result.stderr, (
        f"expected stderr advisory naming the primary path; got: {result.stderr!r}"
    )


def test_export_outside_git_falls_back_to_db_path_root(tmp_path):
    """Outside any git repo, `git rev-parse --show-toplevel` exits non-zero;
    the helper falls back to db_path-derived resolution (the pre-fix path).
    """
    repo, env = _seed_tusk_repo(tmp_path)
    _add_glossary_entry(repo, env, "fallback-test-term", "Fallback marker.")

    # Run from a sibling tmp dir that is NOT inside any git repo. The DB still
    # lives under repo/tusk/tasks.db (pinned via TUSK_DB), so the helper's
    # fallback resolves repo_root to `repo` — same as the pre-fix behavior.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _tusk(["glossary", "export"], cwd=elsewhere, env=env)
    assert result.returncode == 0, (
        f"export failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    primary_path = repo / "docs" / "GLOSSARY.md"
    assert primary_path.exists(), (
        "expected fallback to write to db_path-derived repo's docs/GLOSSARY.md"
    )
    assert "fallback-test-term" in primary_path.read_text(encoding="utf-8")
