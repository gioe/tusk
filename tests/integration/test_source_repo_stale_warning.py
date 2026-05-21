"""Integration tests for the source-repo stale-bin warning (issue #810).

When `tusk` is invoked from the tusk source repo's own bin/tusk and the local
default branch is behind origin's default, every invocation silently runs an
outdated binary that may be missing already-shipped fixes — leading to
phantom-bug investigations (incident: TASK-391 was filed against a local main
that was 29 commits behind origin/main; the bug it described was already fixed
on origin/main).

The fix adds `maybe_warn_source_repo_stale` to bin/tusk, gated identically to
the cross-repo drift warning (TUSK_QUIET / non-TTY stderr / TUSK_FORCE_WARN).

These tests construct a self-contained fake source-repo skeleton with a bare
origin one commit ahead, copy bin/tusk into it, then drive the warning under
each gating condition.
"""

import os
import shutil
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN_SOURCE = os.path.join(REPO_ROOT, "bin", "tusk")
WARNING_FRAGMENT = "local source bin/tusk is"


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


def _seed_fake_source_repo(tmp_path, lag=True):
    """Build a minimal fake-source-repo + bare-origin layout.

    Returns the local repo path. If lag=True, the local default branch is one
    commit behind origin's default after fetch.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main"],
        cwd=bare,
        check=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(local)],
        check=True,
        capture_output=True,
    )
    _git(["config", "user.email", "tusk@example.test"], cwd=local)
    _git(["config", "user.name", "Tusk Tests"], cwd=local)

    bin_dir = local / "bin"
    bin_dir.mkdir()
    shutil.copy(TUSK_BIN_SOURCE, bin_dir / "tusk")
    os.chmod(bin_dir / "tusk", 0o755)
    (local / "config.default.json").write_text("{}\n", encoding="utf-8")
    (local / "VERSION").write_text("1\n", encoding="utf-8")
    (local / "README.md").write_text("fake-source\n", encoding="utf-8")
    _git(["add", "-A"], cwd=local)
    _git(["commit", "-q", "-m", "init"], cwd=local)
    _git(["push", "-q", "-u", "origin", "main"], cwd=local)
    _git(["remote", "set-head", "origin", "-a"], cwd=local)

    if lag:
        helper = tmp_path / "helper"
        subprocess.run(
            ["git", "clone", "-q", str(bare), str(helper)],
            check=True,
            capture_output=True,
        )
        _git(["config", "user.email", "tusk@example.test"], cwd=helper)
        _git(["config", "user.name", "Tusk Tests"], cwd=helper)
        (helper / "new.txt").write_text("advance\n", encoding="utf-8")
        _git(["add", "new.txt"], cwd=helper)
        _git(["commit", "-q", "-m", "advance origin"], cwd=helper)
        _git(["push", "-q", "origin", "main"], cwd=helper)
        _git(["fetch", "-q", "origin"], cwd=local)

    return local


def _tusk_version(local, *, env):
    return subprocess.run(
        [str(local / "bin" / "tusk"), "version"],
        cwd=local,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _env(extra=None):
    base = os.environ.copy()
    base["TUSK_DB"] = "/tmp/tusk-stale-warning-tests-no-db.db"
    if extra:
        base.update(extra)
    return base


def test_warning_fires_when_local_main_behind_origin(tmp_path):
    local = _seed_fake_source_repo(tmp_path, lag=True)

    result = _tusk_version(local, env=_env({"TUSK_FORCE_WARN": "1"}))

    assert result.returncode == 0
    assert WARNING_FRAGMENT in result.stderr
    assert "behind origin/main" in result.stderr
    assert "1 commits behind" in result.stderr


def test_no_warning_when_local_main_up_to_date(tmp_path):
    local = _seed_fake_source_repo(tmp_path, lag=False)

    result = _tusk_version(local, env=_env({"TUSK_FORCE_WARN": "1"}))

    assert result.returncode == 0
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_suppressed_by_tusk_quiet(tmp_path):
    local = _seed_fake_source_repo(tmp_path, lag=True)

    result = _tusk_version(
        local, env=_env({"TUSK_QUIET": "1", "TUSK_FORCE_WARN": "1"})
    )

    assert result.returncode == 0
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_suppressed_in_non_tty_without_force_warn(tmp_path):
    """When stderr isn't a TTY and TUSK_FORCE_WARN isn't set, the warning is silent."""
    local = _seed_fake_source_repo(tmp_path, lag=True)

    env = _env()
    env.pop("TUSK_FORCE_WARN", None)
    result = _tusk_version(local, env=env)

    assert result.returncode == 0
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_skipped_for_consumer_install(tmp_path):
    """Without INSTALL_DIR/config.default.json the warning is skipped (consumer)."""
    local = _seed_fake_source_repo(tmp_path, lag=True)
    (local / "config.default.json").unlink()

    result = _tusk_version(local, env=_env({"TUSK_FORCE_WARN": "1"}))

    assert result.returncode == 0
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_gracefully_skipped_when_origin_head_unknown(tmp_path):
    """If origin/HEAD isn't set locally (never fetched / fresh clone), skip silently."""
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main"], cwd=bare, check=True
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(local)],
        check=True,
        capture_output=True,
    )
    _git(["config", "user.email", "tusk@example.test"], cwd=local)
    _git(["config", "user.name", "Tusk Tests"], cwd=local)
    (local / "bin").mkdir()
    shutil.copy(TUSK_BIN_SOURCE, local / "bin" / "tusk")
    os.chmod(local / "bin" / "tusk", 0o755)
    (local / "config.default.json").write_text("{}\n", encoding="utf-8")
    (local / "VERSION").write_text("1\n", encoding="utf-8")
    _git(["add", "-A"], cwd=local)
    _git(["commit", "-q", "-m", "init"], cwd=local)
    # Deliberately do NOT push or set-head — origin/HEAD is unknown.
    subprocess.run(
        ["git", "-C", str(local), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
        capture_output=True,
    )

    result = _tusk_version(local, env=_env({"TUSK_FORCE_WARN": "1"}))

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT not in result.stderr
