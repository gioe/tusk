"""Integration tests for the tusk version-bump double-bump guard (issue #1109).

`tusk version-bump` unconditionally increments VERSION with no safeguard
against an accidental double-bump: running it twice silently lands VERSION two
or more ahead of what is committed on origin's default branch, even though the
project rule is exactly one VERSION bump per PR. The over-bump observed during
TASK-672 (origin main at 1167, two bumps produced 1169) was only caught by
manual inspection.

The fix resolves the VERSION committed on origin/<default>
(`git show origin/<default>:VERSION`, falling back to the local default branch
when origin is unreachable) and emits a one-line, non-blocking stderr warning
when the new value exceeds that baseline by more than 1.

Cases covered:
- A single bump (the normal case) emits no warning and exits 0.
- A second consecutive bump lands +2 ahead and warns "ahead of origin",
  while still exiting 0 and staging the bumped VERSION (non-blocking).
- The baseline falls back to the local default branch when no origin remote
  exists, so the warning still fires on a double-bump.
"""

import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

_WARN_NEEDLE = "ahead of origin"


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


def _init_repo(repo, *, initial):
    os.makedirs(repo, exist_ok=True)
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write(f"{initial}\n")
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
        f.write("seed\n")
    _git(["add", "VERSION", "README.md"], cwd=repo)
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


def test_single_bump_emits_no_warning(tmp_path):
    """The normal case — exactly 1 ahead of origin — must not warn."""
    repo = _seed_repo_with_origin(tmp_path, initial="100")
    env = _env_with_db(repo)

    result = _tusk(["version-bump"], cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert open(os.path.join(repo, "VERSION")).read().strip() == "101"
    assert _WARN_NEEDLE not in result.stderr.lower(), (
        f"single bump (1 ahead) must not warn; stderr was:\n{result.stderr}"
    )


def test_double_bump_warns_and_is_non_blocking(tmp_path):
    """Two consecutive bumps land +2 ahead of origin and must warn — but the
    bump still succeeds (exit 0, VERSION staged), since the guard is advisory.
    """
    repo = _seed_repo_with_origin(tmp_path, initial="100")
    env = _env_with_db(repo)

    first = _tusk(["version-bump"], cwd=repo, env=env)
    assert first.returncode == 0, first.stderr
    assert _WARN_NEEDLE not in first.stderr.lower(), (
        f"first bump must not warn; stderr was:\n{first.stderr}"
    )

    second = _tusk(["version-bump"], cwd=repo, env=env)

    # Non-blocking: still exits 0 and still stages the bumped VERSION.
    assert second.returncode == 0, second.stderr
    assert open(os.path.join(repo, "VERSION")).read().strip() == "102"
    assert "VERSION" in _staged_files(repo)
    # Loud: the warning fires and names the over-bump.
    assert _WARN_NEEDLE in second.stderr.lower(), (
        f"second bump (2 ahead) must warn; stderr was:\n{second.stderr}"
    )


def test_double_bump_warns_via_local_default_when_no_origin(tmp_path):
    """When no origin remote exists, the baseline falls back to the local
    default branch's committed VERSION, so the double-bump still warns.
    """
    repo = str(tmp_path / "repo")
    _init_repo(repo, initial="100")  # on main, no origin remote
    env = _env_with_db(repo)

    first = _tusk(["version-bump"], cwd=repo, env=env)
    assert first.returncode == 0, first.stderr
    assert _WARN_NEEDLE not in first.stderr.lower(), (
        f"first bump must not warn; stderr was:\n{first.stderr}"
    )

    second = _tusk(["version-bump"], cwd=repo, env=env)
    assert second.returncode == 0, second.stderr
    assert _WARN_NEEDLE in second.stderr.lower(), (
        f"second bump must warn via local-default fallback; stderr was:\n{second.stderr}"
    )
