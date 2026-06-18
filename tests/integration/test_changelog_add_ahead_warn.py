"""Integration tests for the tusk changelog-add double-bump guard (issue #1111).

Follow-up to issue #1109 (TASK-673), which added a non-blocking 'ahead of
origin' warning to `tusk version-bump`. `tusk changelog-add` had no equivalent
guard, so an accidental double-bump produced a CHANGELOG heading two or more
ahead of origin's default branch just as silently as the pre-#1109 version-bump
did. The two commands are normally run together, so changelog-add now reuses
`resolve_default_baseline_version` from the version-bump guard and emits the
same warning when the resolved version exceeds the origin/<default> baseline by
more than 1.

Cases covered:
- An in-bounds version (exactly 1 ahead of origin) emits no warning, exits 0,
  and still prepends + stages the CHANGELOG entry.
- A version 2 ahead of origin warns "ahead of origin", while still exiting 0 and
  staging the entry (the guard is advisory / non-blocking).
- The baseline falls back to the local default branch when no origin remote
  exists, so the warning still fires on a double-bump.
- When the baseline cannot be resolved at all (no origin ref and no committed
  VERSION on the default branch), no warning is emitted.
- End-to-end: two `tusk version-bump` calls followed by `tusk changelog-add`
  warns (mirrors the issue's failing-test reproduction).
"""

import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

_WARN_NEEDLE = "ahead of origin"

_CHANGELOG_SEED = "# Changelog\n\n## [Unreleased]\n"


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


def _env_with_db(repo):
    env = os.environ.copy()
    env["TUSK_DB"] = str(os.path.join(repo, "tusk", "tasks.db"))
    env["TUSK_QUIET"] = "1"
    return env


def _write_version(repo, value):
    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write(f"{value}\n")


def _init_repo(repo, *, initial, commit_version=True):
    """Init a repo with CHANGELOG.md (always committed) and VERSION.

    When commit_version is True the VERSION file is committed at <initial>;
    otherwise it is left untracked so the default-branch baseline is
    unresolvable.
    """
    os.makedirs(repo, exist_ok=True)
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    with open(os.path.join(repo, "CHANGELOG.md"), "w", encoding="utf-8") as f:
        f.write(_CHANGELOG_SEED)
    _write_version(repo, initial)
    to_commit = ["CHANGELOG.md"]
    if commit_version:
        to_commit.append("VERSION")
    _git(["add", *to_commit], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)


def _seed_repo_with_origin(tmp_path, *, initial):
    """Repo whose origin/main:VERSION == initial (committed and pushed)."""
    repo = str(tmp_path / "repo")
    origin = str(tmp_path / "origin.git")
    _init_repo(repo, initial=initial)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", origin],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(["remote", "add", "origin", origin], cwd=repo)
    _git(["push", "-u", "origin", "main"], cwd=repo)
    return repo


def _staged_files(repo):
    return subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()


def test_in_bounds_version_emits_no_warning(tmp_path):
    """Exactly 1 ahead of origin — the normal case — must not warn, and the
    entry must still be written and staged."""
    repo = _seed_repo_with_origin(tmp_path, initial="100")
    env = _env_with_db(repo)
    _write_version(repo, "101")

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert _WARN_NEEDLE not in result.stderr.lower(), (
        f"1 ahead must not warn; stderr was:\n{result.stderr}"
    )
    assert "## [101]" in open(os.path.join(repo, "CHANGELOG.md")).read()
    assert "CHANGELOG.md" in _staged_files(repo)


def test_two_ahead_warns_and_is_non_blocking(tmp_path):
    """A version 2 ahead of origin must warn — but changelog-add still exits 0
    and still prepends + stages the entry, since the guard is advisory."""
    repo = _seed_repo_with_origin(tmp_path, initial="100")
    env = _env_with_db(repo)
    _write_version(repo, "102")

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    # Non-blocking: still exits 0 and still stages the prepended entry.
    assert result.returncode == 0, result.stderr
    assert "## [102]" in open(os.path.join(repo, "CHANGELOG.md")).read()
    assert "CHANGELOG.md" in _staged_files(repo)
    # Loud: the warning fires and names the over-bump.
    assert _WARN_NEEDLE in result.stderr.lower(), (
        f"2 ahead must warn; stderr was:\n{result.stderr}"
    )


def test_two_ahead_warns_via_local_default_when_no_origin(tmp_path):
    """When no origin remote exists, the baseline falls back to the local
    default branch's committed VERSION, so the over-bump still warns."""
    repo = str(tmp_path / "repo")
    _init_repo(repo, initial="100")  # on main, no origin remote
    env = _env_with_db(repo)
    _write_version(repo, "102")

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert _WARN_NEEDLE in result.stderr.lower(), (
        f"2 ahead must warn via local-default fallback; stderr was:\n{result.stderr}"
    )


def test_no_warning_when_baseline_unresolvable(tmp_path):
    """With no origin ref and no committed VERSION on the default branch, the
    baseline is unresolvable, so no warning is emitted even when the file
    version is far ahead — and changelog-add still succeeds."""
    repo = str(tmp_path / "repo")
    _init_repo(repo, initial="100", commit_version=False)  # VERSION untracked
    env = _env_with_db(repo)
    _write_version(repo, "999")

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert _WARN_NEEDLE not in result.stderr.lower(), (
        f"unresolvable baseline must not warn; stderr was:\n{result.stderr}"
    )
    assert "## [999]" in open(os.path.join(repo, "CHANGELOG.md")).read()


def test_end_to_end_double_bump_then_changelog_add_warns(tmp_path):
    """Mirrors the issue's failing test: two version-bumps land VERSION at +2,
    and the subsequent changelog-add warns 'ahead of origin'."""
    repo = _seed_repo_with_origin(tmp_path, initial="100")
    env = _env_with_db(repo)

    assert _tusk(["version-bump"], cwd=repo, env=env).returncode == 0
    assert _tusk(["version-bump"], cwd=repo, env=env).returncode == 0
    assert open(os.path.join(repo, "VERSION")).read().strip() == "102"

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert _WARN_NEEDLE in result.stderr.lower(), (
        f"changelog-add after double-bump must warn; stderr was:\n{result.stderr}"
    )
