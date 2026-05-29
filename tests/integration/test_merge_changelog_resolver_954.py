"""Real-git tests for _recover_version_changelog_rebase_conflict (issues #951, #954).

#951: the resolver must fire when only CHANGELOG.md conflicts (the same-version
parallel race auto-merges VERSION), not just when both VERSION and CHANGELOG.md
conflict.

#954: when the resolver fires, the resulting CHANGELOG.md must keep a single
'# Changelog' title with no version block above it — the old code prepended the
new entry above upstream's title and stacked duplicate titles on every merge.
"""

import importlib.util
import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(REPO_ROOT, "bin", f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_merge = _load("tusk-merge")

_PREAMBLE = (
    "# Changelog\n\n"
    "All notable changes to tusk are documented in this file.\n\n"
    "Format based on [Keep a Changelog](https://keepachangelog.com/), "
    "adapted for integer versioning.\n\n"
    "## [Unreleased]\n\n"
)


def _changelog(entries):
    """entries: list of (version, bullet). Newest first."""
    blocks = [f"## [{v}] - 2026-05-28\n\n- {bullet}\n" for v, bullet in entries]
    return _PREAMBLE + "\n".join(blocks)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _write(repo, name, content):
    with open(os.path.join(repo, name), "w", encoding="utf-8") as fh:
        fh.write(content)


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.editor", "true")
    _git(repo, "config", "commit.gpgsign", "false")


def _setup_conflict(repo, upstream_version, task_version):
    """Build base -> upstream / task branches and leave a paused rebase conflict."""
    _init_repo(repo)
    _write(repo, "VERSION", "1038\n")
    _write(repo, "CHANGELOG.md", _changelog([("1038", "base")]))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_branch = _git(repo, "branch", "--show-current").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "upstream")
    _write(repo, "VERSION", f"{upstream_version}\n")
    _write(
        repo,
        "CHANGELOG.md",
        _changelog([(str(upstream_version), "upstream entry"), ("1038", "base")]),
    )
    _git(repo, "commit", "-qam", "upstream bump")

    _git(repo, "checkout", "-q", base_branch)
    _git(repo, "checkout", "-q", "-b", "task")
    _write(repo, "VERSION", f"{task_version}\n")
    _write(
        repo,
        "CHANGELOG.md",
        _changelog([(str(task_version), "task entry"), ("1038", "base")]),
    )
    _git(repo, "commit", "-qam", "task bump")

    _git(repo, "rebase", "upstream", check=False)
    return repo


def _unmerged(repo):
    out = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _title_count(content):
    return sum(1 for line in content.splitlines() if line == "# Changelog")


def _has_version_above_title(content):
    lines = content.splitlines()
    title_idx = next(i for i, line in enumerate(lines) if line == "# Changelog")
    return any(line.startswith("## [") for line in lines[:title_idx])


def test_fires_on_changelog_only_conflict_same_version_race(tmp_path):
    """#951: both branches bump to the SAME version -> VERSION auto-merges,
    only CHANGELOG.md conflicts; the resolver must still fire."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _setup_conflict(repo, upstream_version=1039, task_version=1039)

    # Precondition: this is the case the old guard rejected.
    assert _unmerged(repo) == {"CHANGELOG.md"}

    assert tusk_merge._recover_version_changelog_rebase_conflict(repo, "upstream") is True

    with open(os.path.join(repo, "VERSION"), encoding="utf-8") as fh:
        assert fh.read().strip() == "1040"
    with open(os.path.join(repo, "CHANGELOG.md"), encoding="utf-8") as fh:
        changelog = fh.read()
    assert _title_count(changelog) == 1
    assert not _has_version_above_title(changelog)
    assert "## [1040] - 2026-05-28" in changelog
    # rebase completed cleanly
    assert _unmerged(repo) == set()


def test_no_corruption_on_both_conflict(tmp_path):
    """#954: VERSION + CHANGELOG both conflict; resolver must not prepend a
    version block above the title or duplicate the '# Changelog' header."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _setup_conflict(repo, upstream_version=1040, task_version=1039)

    assert _unmerged(repo) == {"VERSION", "CHANGELOG.md"}

    assert tusk_merge._recover_version_changelog_rebase_conflict(repo, "upstream") is True

    with open(os.path.join(repo, "VERSION"), encoding="utf-8") as fh:
        assert fh.read().strip() == "1041"
    with open(os.path.join(repo, "CHANGELOG.md"), encoding="utf-8") as fh:
        changelog = fh.read()
    assert _title_count(changelog) == 1
    assert not _has_version_above_title(changelog)
    assert changelog.startswith("# Changelog")
    assert "## [1041] - 2026-05-28" in changelog
    # task entry reassigned to 1041 sits above upstream's 1040 entry
    assert changelog.index("## [1041]") < changelog.index("## [1040]")
    assert _unmerged(repo) == set()


def test_does_not_fire_on_unrelated_conflict(tmp_path):
    """Guard still returns False when an unrelated file is also conflicted."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _init_repo(repo)
    _write(repo, "VERSION", "1038\n")
    _write(repo, "CHANGELOG.md", _changelog([("1038", "base")]))
    _write(repo, "other.txt", "base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_branch = _git(repo, "branch", "--show-current").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "upstream")
    _write(repo, "other.txt", "upstream\n")
    _git(repo, "commit", "-qam", "upstream other")

    _git(repo, "checkout", "-q", base_branch)
    _git(repo, "checkout", "-q", "-b", "task")
    _write(repo, "other.txt", "task\n")
    _git(repo, "commit", "-qam", "task other")
    _git(repo, "rebase", "upstream", check=False)

    assert "other.txt" in _unmerged(repo)
    assert tusk_merge._recover_version_changelog_rebase_conflict(repo, "upstream") is False
