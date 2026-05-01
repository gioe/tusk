"""Regression tests for TASK-264 / Issue #631 and TASK-265 / Issue #634.

`_version_bump_check` (Rules 13/20 — VERSION-bump-missing advisories) has two
parts and two distinct suppression guards:

  * Part A (uncommitted) suppresses via ``just_bumped`` — HEAD is the most
    recent commit that touched VERSION. Covers the moment between the
    bump-only commit and the follow-up feature commit on the same branch
    (Issue #631).
  * Part B (committed since last bump) suppresses via ``bump_is_recent`` —
    the bump is within ``_BUMP_RECENT_WINDOW`` commits of HEAD on the linear
    history. Covers the post-merge state of a typical split-bump PR
    (bump → feature commit(s) → merge), so Part B does not keep firing on
    every developer's tree until the next bump (Issue #634).

The over-application guards (no bump anywhere recent → Part A still fires;
bump older than the window → Part B still fires) are exercised explicitly so
the suppressions cannot drift into always-on.
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
# Part B suppressed inside the bump_is_recent window (TASK-265 / Issue #634)
# ---------------------------------------------------------------------------


def test_part_b_suppressed_for_post_split_bump_committed_change(tmp_path):
    """Updated criterion 1170 — TASK-265 / Issue #634.

    `_version_bump_check` Part B must NOT fire when a `bin/tusk-*.py` change
    is committed on top of a split-bump-pattern bump (bump-commit →
    feature-commit, both committed) and the bump is within the
    `bump_is_recent` window of HEAD. Pre-TASK-265 this was the persistent
    advisory that polluted lint output on every developer's tree until the
    next task's bump landed.
    """
    repo = str(tmp_path / "repo")
    _git_init(repo)
    _seed_minimum_repo(repo, version="1")

    # Split-bump pattern: VERSION-only commit, then the feature commit on top.
    _bump_version_commit(repo, new_version="2")
    with open(os.path.join(repo, "bin", "tusk-sample.py"), "a") as f:
        f.write("# committed feature edit\n")
    _git(repo, "add", "bin/tusk-sample.py")
    _git(repo, "commit", "-q", "-m", "[TASK-X] feature edit")

    result = _run_lint(repo)

    assert "Committed since last VERSION bump" not in result.stdout, (
        "Part B must suppress the advisory for a feature commit landed on top "
        "of an immediately-preceding split-bump commit (Issue #634). Output:\n"
        f"{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Issue #634 minimal repro — bump → feature, both committed (criterion 1173)
# ---------------------------------------------------------------------------


def test_issue_634_split_bump_repro(tmp_path):
    """Exact scenario from Issue #634's `## Failing Test` section: after the
    split-bump merges, `tusk lint` on the resulting tree must not emit
    "Committed since last VERSION bump".
    """
    repo = str(tmp_path / "repo")
    _git_init(repo)
    _seed_minimum_repo(repo, version="1")

    # bump → feature, exactly as the issue's failing test constructs it.
    _bump_version_commit(repo, new_version="2")
    with open(os.path.join(repo, "bin", "tusk-sample.py"), "a") as f:
        f.write("# change\n")
    _git(repo, "add", "bin/tusk-sample.py")
    _git(repo, "commit", "-q", "-m", "feature")

    result = _run_lint(repo)

    assert "Committed since last VERSION bump" not in result.stdout, (
        "Issue #634 repro: post-split-bump feature commit must not trip "
        "Part B's advisory. Output:\n"
        f"{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Window overflow — Part B still fires when the bump is far back (criterion 1174)
# ---------------------------------------------------------------------------


def test_part_b_still_fires_when_bump_outside_recent_window(tmp_path):
    """The `bump_is_recent` window must not over-apply: a `bin/tusk-*.py`
    edit committed many commits after the bump (beyond the window) must
    still trip Part B's advisory. This is the genuine "you're way past your
    last bump — bump again before merging" signal that Part B preserves.
    """
    repo = str(tmp_path / "repo")
    _git_init(repo)
    _seed_minimum_repo(repo, version="1")

    _bump_version_commit(repo, new_version="2")

    # Commit far more changes than the recent-window allows. _BUMP_RECENT_WINDOW
    # is 10 in the source; 15 commits is comfortably beyond it without coupling
    # the test to the exact constant.
    sample = os.path.join(repo, "bin", "tusk-sample.py")
    for i in range(15):
        with open(sample, "a") as f:
            f.write(f"# commit {i}\n")
        _git(repo, "add", "bin/tusk-sample.py")
        _git(repo, "commit", "-q", "-m", f"[TASK-X] feature {i}")

    result = _run_lint(repo)

    assert "Committed since last VERSION bump" in result.stdout, (
        "Part B must still fire when the bump is well outside the recent "
        "window — the suppression must not over-apply. Output:\n"
        f"{result.stdout}"
    )
