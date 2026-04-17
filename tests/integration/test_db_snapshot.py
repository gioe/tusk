"""Regression: pre-command DB snapshots defend against silent clobbers.

If an external operation (git stash pop of an old stash, rm, git checkout
across the un-track commit, etc.) replaces or deletes tusk/tasks.db, the
most recent snapshot must still be recoverable from tusk/backups/. These
tests pin the behavior of the snapshot hook added to bin/tusk.
"""

import os
import subprocess
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "init"],
        cwd=str(repo), check=True,
    )
    return repo


def _env_for(repo, tmp_path, **overrides):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TUSK_STATE_DIR": str(tmp_path / "state"),
        "TUSK_PROJECT": str(repo),
    }
    env.pop("TUSK_DB", None)
    env.update(overrides)
    return env


def _init_db(repo, env):
    r = subprocess.run(
        [TUSK_BIN, "init"], cwd=str(repo), env=env,
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def _list_backups(repo):
    backups = repo / "tusk" / "backups"
    if not backups.exists():
        return []
    return sorted(p.name for p in backups.glob("tasks.db.*"))


def test_mutating_command_creates_snapshot(tmp_path):
    repo = _make_repo(tmp_path)
    env = _env_for(repo, tmp_path)
    _init_db(repo, env)
    before = _list_backups(repo)

    r = subprocess.run(
        [TUSK_BIN, "task-insert", "S", "D", "--complexity", "S", "--criteria", "C"],
        cwd=str(repo), env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    after = _list_backups(repo)
    assert len(after) == len(before) + 1, (
        f"expected one new snapshot, got before={before} after={after}"
    )


def test_readonly_command_does_not_snapshot(tmp_path):
    repo = _make_repo(tmp_path)
    env = _env_for(repo, tmp_path)
    _init_db(repo, env)
    # Prime one snapshot so the dir exists and we're measuring delta, not creation.
    subprocess.run(
        [TUSK_BIN, "task-insert", "Seed", "D", "--complexity", "S", "--criteria", "C"],
        cwd=str(repo), env=env, check=True, capture_output=True,
    )
    before = _list_backups(repo)

    for subcmd in ["path", "config", "task-list", "version"]:
        r = subprocess.run(
            [TUSK_BIN, subcmd], cwd=str(repo), env=env,
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{subcmd}: {r.stderr}"

    after = _list_backups(repo)
    assert after == before, (
        f"read-only subcommands must not snapshot: before={before} after={after}"
    )


def test_retention_rotates_oldest(tmp_path):
    repo = _make_repo(tmp_path)
    env = _env_for(repo, tmp_path, TUSK_BACKUP_RETENTION="2")
    _init_db(repo, env)

    # Create 4 mutations with 1s gaps so timestamps sort stably.
    for i in range(4):
        subprocess.run(
            [TUSK_BIN, "task-insert", f"T{i}", "D", "--complexity", "S", "--criteria", "C"],
            cwd=str(repo), env=env, check=True, capture_output=True,
        )
        time.sleep(1)

    backups = _list_backups(repo)
    assert len(backups) == 2, f"retention=2 must cap at 2 files, got {backups}"
    # Newest two remain: the sort-order (lex on YYYYMMDDHHMMSS) equals chronological.
    assert backups == sorted(backups)


def test_opt_out_suppresses_snapshot(tmp_path):
    repo = _make_repo(tmp_path)
    env = _env_for(repo, tmp_path)
    _init_db(repo, env)
    # Prime to ensure dir exists; use default backup mode so the seed itself snapshots.
    subprocess.run(
        [TUSK_BIN, "task-insert", "Seed", "D", "--complexity", "S", "--criteria", "C"],
        cwd=str(repo), env=env, check=True, capture_output=True,
    )
    before = _list_backups(repo)

    env_optout = {**env, "TUSK_NO_BACKUP": "1"}
    r = subprocess.run(
        [TUSK_BIN, "task-insert", "Opt", "D", "--complexity", "S", "--criteria", "C"],
        cwd=str(repo), env=env_optout, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    after = _list_backups(repo)
    assert after == before, (
        f"TUSK_NO_BACKUP=1 must suppress snapshot: before={before} after={after}"
    )


def test_snapshot_is_recoverable(tmp_path):
    """The recovery scenario that motivated this defense: simulate an external
    process clobbering tusk/tasks.db and verify the snapshot restores real data.

    Snapshots fire *before* dispatch, so the most recent snapshot reflects the
    state before the last mutation. To verify canary data survives, insert the
    canary, then run another mutating command — that second command's
    pre-snapshot captures the canary.
    """
    repo = _make_repo(tmp_path)
    env = _env_for(repo, tmp_path)
    _init_db(repo, env)

    subprocess.run(
        [TUSK_BIN, "task-insert", "canary summary", "D",
         "--complexity", "S", "--criteria", "C"],
        cwd=str(repo), env=env, check=True, capture_output=True,
    )
    # Second mutation — its pre-snapshot captures the canary from the first.
    subprocess.run(
        [TUSK_BIN, "task-insert", "second", "D",
         "--complexity", "S", "--criteria", "C"],
        cwd=str(repo), env=env, check=True, capture_output=True,
    )

    db = repo / "tusk" / "tasks.db"
    backups = _list_backups(repo)
    assert len(backups) >= 2, f"expected >=2 snapshots, got {backups}"

    # Simulate the incident: an external clobber replaces the DB with an
    # empty SQLite file (same pattern as a stale `git stash pop`).
    db.unlink()
    subprocess.run(["sqlite3", str(db), "SELECT 1;"], check=True, capture_output=True)

    # Recovery: copy the newest snapshot back over tasks.db.
    newest = sorted((repo / "tusk" / "backups").glob("tasks.db.*"))[-1]
    db.write_bytes(newest.read_bytes())

    r = subprocess.run(
        [TUSK_BIN, "task-list", "--format", "json"], cwd=str(repo), env=env,
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "canary summary" in r.stdout, (
        f"restore must bring the canary task back: {r.stdout!r}"
    )
