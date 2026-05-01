"""Regression tests for TASK-264 / Issue #631.

`_version_bump_check` Part A (Rules 13/20 — VERSION-bump-missing advisories)
must suppress its violation when the immediately-preceding commit on HEAD is
the VERSION bump itself. This is the documented "split the bump into its own
commit" workflow from CLAUDE.md — a follow-up feature commit on the same
branch should not re-flag the advisory.

The opposite case (a `bin/tusk-*.py` or `skills/` change without any prior
bump) must continue to fire the advisory — guarded explicitly to prevent the
suppression from over-applying.
"""

import os
import subprocess

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_LINT_PY = os.path.join(REPO_ROOT, "bin", "tusk-lint.py")


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git_init(repo: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _seed_minimum_repo(repo: str, version: str = "1") -> None:
    """Plant a bin/ tree that survives the surrounding lint rules so only the
    Rules 13/20 advisory has a chance to fire on subsequent edits.
    """
    bin_dir = os.path.join(repo, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    tusk_shim = os.path.join(bin_dir, "tusk")
    with open(tusk_shim, "w") as f:
        f.write("#!/bin/bash\n# references tusk-sample.py\nexit 0\n")
    os.chmod(tusk_shim, 0o755)
    with open(os.path.join(bin_dir, "tusk-sample.py"), "w") as f:
        f.write("# sample\n")
    with open(os.path.join(bin_dir, "dist-excluded.txt"), "w") as f:
        f.write("")
    with open(os.path.join(repo, "VERSION"), "w") as f:
        f.write(f"{version}\n")
    import json as _json
    manifest_entries = [
        ".claude/bin/tusk",
        ".claude/bin/tusk-sample.py",
        ".claude/bin/config.default.json",
        ".claude/bin/VERSION",
        ".claude/bin/pricing.json",
    ]
    with open(os.path.join(repo, "MANIFEST"), "w") as f:
        _json.dump(manifest_entries, f)
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    with open(os.path.join(repo, ".claude", "tusk-manifest.json"), "w") as f:
        _json.dump(manifest_entries, f)
    _git(
        repo,
        "add",
        "bin/tusk",
        "bin/tusk-sample.py",
        "bin/dist-excluded.txt",
        "VERSION",
        "MANIFEST",
        ".claude/tusk-manifest.json",
    )
    _git(repo, "commit", "-q", "-m", "seed")


def _run_lint(repo: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", TUSK_LINT_PY, repo, "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _bump_version_commit(repo: str, new_version: str) -> None:
    """Simulate the documented split-bump commit: VERSION+CHANGELOG-only."""
    with open(os.path.join(repo, "VERSION"), "w") as f:
        f.write(f"{new_version}\n")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-q", "-m", "[TASK-X] Bump VERSION")


# ---------------------------------------------------------------------------
# Suppression case (criterion 1167)
# ---------------------------------------------------------------------------


def test_advisory_suppressed_when_version_bumped_in_prior_commit(tmp_path):
    """Multi-commit feature branch following the split-bump workflow must not
    fire the Rules 13/20 advisory on the follow-up feature commit.
    """
    repo = str(tmp_path / "repo")
    _git_init(repo)
    _seed_minimum_repo(repo, version="1")

    # Step 1: bump VERSION in its own commit (split-bump pattern).
    _bump_version_commit(repo, new_version="2")

    # Step 2: stage a follow-up bin/tusk-*.py change WITHOUT bumping VERSION
    # again. Without the just-bumped guard, Rule 13's Part A would fire.
    with open(os.path.join(repo, "bin", "tusk-sample.py"), "a") as f:
        f.write("# follow-up edit\n")

    result = _run_lint(repo)

    assert "modified without VERSION bump" not in result.stdout, (
        "Rules 13/20 must suppress the advisory when the prior commit was "
        "the VERSION bump (split-bump workflow). Output:\n"
        f"{result.stdout}"
    )


# ---------------------------------------------------------------------------
# No-regression case (criterion 1168)
# ---------------------------------------------------------------------------


def test_advisory_still_fires_when_version_not_recently_bumped(tmp_path):
    """A `bin/tusk-*.py` change with no recent VERSION bump must still fire
    the advisory — the suppression must not over-apply.
    """
    repo = str(tmp_path / "repo")
    _git_init(repo)
    _seed_minimum_repo(repo, version="1")

    # Plant an unrelated commit so the most recent commit on HEAD did NOT
    # touch VERSION.
    other = os.path.join(repo, "bin", "tusk-other.py")
    with open(other, "w") as f:
        f.write("# unrelated\n")
    _git(repo, "add", "bin/tusk-other.py")
    _git(repo, "commit", "-q", "-m", "[TASK-X] unrelated change")

    # Stage a follow-up bin/tusk-*.py change without a VERSION bump.
    with open(os.path.join(repo, "bin", "tusk-sample.py"), "a") as f:
        f.write("# follow-up edit\n")

    result = _run_lint(repo)

    assert "modified without VERSION bump" in result.stdout, (
        "Rules 13/20 must still fire when no VERSION bump is in HEAD's "
        "ancestry. Output:\n"
        f"{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Part B unchanged — committed-since-last-bump path still flags (criterion 1170)
# ---------------------------------------------------------------------------


def test_part_b_still_flags_committed_changes_without_bump(tmp_path):
    """`_version_bump_check` Part B must still fire when a `bin/tusk-*.py`
    change has been committed *since* the last VERSION bump (i.e., the bump
    is not on HEAD).
    """
    repo = str(tmp_path / "repo")
    _git_init(repo)
    _seed_minimum_repo(repo, version="1")

    # Bump VERSION in its own commit, then commit an unrelated bin/tusk-*.py
    # edit on top — that edit should trigger Part B's advisory because the
    # bump is no longer on HEAD.
    _bump_version_commit(repo, new_version="2")
    with open(os.path.join(repo, "bin", "tusk-sample.py"), "a") as f:
        f.write("# committed feature edit\n")
    _git(repo, "add", "bin/tusk-sample.py")
    _git(repo, "commit", "-q", "-m", "[TASK-X] feature edit")

    result = _run_lint(repo)

    assert "Committed since last VERSION bump" in result.stdout, (
        "Part B must still flag bin/tusk-*.py edits committed after the "
        "VERSION bump. Output:\n"
        f"{result.stdout}"
    )
