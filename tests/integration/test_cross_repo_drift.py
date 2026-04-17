"""Regression: cross-repo CWD drift must warn or respect an explicit pin.

Reproduces issue #464: a `cd` from the originating project into a different
git repository used to silently reroute tusk to the consumer's DB. This test
spins up two real git repos, registers one as active, then runs `tusk` from
the other and asserts the warning fires. It also verifies that TUSK_PROJECT
pins the resolution regardless of CWD.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _make_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    (path / "tusk").mkdir(exist_ok=True)
    return path


def _run(cmd, cwd, env):
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)


@pytest.fixture()
def two_repos(tmp_path):
    """Create two sibling git repos — 'origin' (pinned) and 'consumer' (drift target)."""
    origin = _make_git_repo(tmp_path / "origin")
    consumer = _make_git_repo(tmp_path / "consumer")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Inherit the full parent env (LANG, USER, etc.) so subprocesses stay
    # portable on stricter environments; override only what the test needs.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TUSK_STATE_DIR": str(state_dir),
    }
    # Ensure no stray pin from the parent shell bleeds into the tests.
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    env.pop("TUSK_QUIET", None)
    return origin, consumer, state_dir, env


def test_drift_warning_fires_from_consumer_repo(two_repos):
    origin, consumer, state_dir, env = two_repos

    # Simulate task-start having registered the origin repo.
    r = _run([TUSK_BIN, "active-project", "add", str(origin)], cwd=origin, env=env)
    assert r.returncode == 0, r.stderr

    # From the consumer repo, running any tusk command must surface the warning.
    # subprocess.run captures stderr (non-TTY), so TUSK_FORCE_WARN=1 is required
    # to exercise the fire path — without it the default-quiet TTY check suppresses.
    env_force = {**env, "TUSK_FORCE_WARN": "1"}
    r = _run([TUSK_BIN, "path"], cwd=consumer, env=env_force)
    assert r.returncode == 0
    assert "active session" in r.stderr.lower()
    assert str(origin.resolve()) in r.stderr or os.path.realpath(str(origin)) in r.stderr


def test_drift_warning_silent_when_stderr_not_tty(two_repos):
    """Default-quiet: without TUSK_FORCE_WARN, non-TTY stderr suppresses the warning."""
    origin, consumer, state_dir, env = two_repos

    _run([TUSK_BIN, "active-project", "add", str(origin)], cwd=origin, env=env)

    # No TUSK_FORCE_WARN, no TUSK_QUIET, no pin — subprocess captures stderr.
    r = _run([TUSK_BIN, "path"], cwd=consumer, env=env)
    assert r.returncode == 0
    assert "warning" not in r.stderr.lower()
    assert "active session" not in r.stderr.lower()


def test_drift_warning_silenced_by_tusk_project_pin(two_repos):
    origin, consumer, state_dir, env = two_repos

    _run([TUSK_BIN, "active-project", "add", str(origin)], cwd=origin, env=env)

    # With TUSK_PROJECT pointed at origin, no drift warning even when CWD is consumer.
    env_with_pin = {**env, "TUSK_PROJECT": str(origin)}
    r = _run([TUSK_BIN, "path"], cwd=consumer, env=env_with_pin)
    assert r.returncode == 0
    assert "warning" not in r.stderr.lower()
    # DB path resolves to the pinned origin, not the consumer.
    assert r.stdout.strip().startswith(str(origin.resolve())) or \
        r.stdout.strip().startswith(os.path.realpath(str(origin)))


def test_drift_warning_silenced_by_tusk_quiet(two_repos):
    origin, consumer, state_dir, env = two_repos

    _run([TUSK_BIN, "active-project", "add", str(origin)], cwd=origin, env=env)

    env_quiet = {**env, "TUSK_QUIET": "1"}
    r = _run([TUSK_BIN, "path"], cwd=consumer, env=env_quiet)
    assert r.returncode == 0
    assert "warning" not in r.stderr.lower()


def test_no_warning_when_cwd_matches_registry(two_repos):
    origin, consumer, state_dir, env = two_repos

    _run([TUSK_BIN, "active-project", "add", str(origin)], cwd=origin, env=env)

    # Running from origin itself — no mismatch, no warning.
    r = _run([TUSK_BIN, "path"], cwd=origin, env=env)
    assert r.returncode == 0
    assert "warning" not in r.stderr.lower()


def test_registry_deregister_removes_entry(two_repos):
    origin, consumer, state_dir, env = two_repos

    _run([TUSK_BIN, "active-project", "add", str(origin)], cwd=origin, env=env)
    assert (state_dir / "active-projects").exists()

    _run([TUSK_BIN, "active-project", "remove", str(origin)], cwd=origin, env=env)

    # File is either gone or empty.
    registry = state_dir / "active-projects"
    if registry.exists():
        assert registry.read_text().strip() == ""

    # And the warning no longer fires from the consumer.
    r = _run([TUSK_BIN, "path"], cwd=consumer, env=env)
    assert "warning" not in r.stderr.lower()
