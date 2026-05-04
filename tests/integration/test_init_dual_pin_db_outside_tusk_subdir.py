"""Regression: tusk init must succeed when TUSK_DB pins the DB outside <project>/tusk/.

Reproduces issue #596: when both TUSK_PROJECT=<target> and TUSK_DB=<target>/test.db
are set, mkdir -p "$DB_DIR" only creates dirname(TUSK_DB) (= $TARGET), leaving
$TARGET/tusk/ uncreated. The cp into $PROJECT_CONFIG (= $TARGET/tusk/config.json)
then failed with "No such file or directory" and aborted under set -euo pipefail.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    return path


def _clean_env(tmp_path: Path) -> dict:
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TUSK_STATE_DIR": str(tmp_path / "state"),
        "TUSK_QUIET": "1",
    }
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    return env


@pytest.fixture()
def source_and_target(tmp_path):
    source = _make_git_repo(tmp_path / "source")
    target = _make_git_repo(tmp_path / "target")
    return source, target, _clean_env(tmp_path)


def test_dual_pin_db_outside_tusk_subdir_succeeds(source_and_target):
    source, target, env = source_and_target
    db_path = target / "test.db"

    env_pinned = {
        **env,
        "TUSK_PROJECT": str(target),
        "TUSK_DB": str(db_path),
    }
    r = subprocess.run(
        [TUSK_BIN, "init"],
        cwd=str(source),
        env=env_pinned,
        capture_output=True,
        text=True,
    )

    assert r.returncode == 0, f"tusk init failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert db_path.exists(), "TUSK_DB path was not created"
    assert (target / "tusk" / "config.json").exists(), \
        "PROJECT_CONFIG was not written under TUSK_PROJECT/tusk/"


def test_tusk_db_only_db_outside_tusk_subdir_succeeds(tmp_path):
    """No regression: TUSK_DB-only (no TUSK_PROJECT) with DB outside cwd's tusk/ still works.

    Without TUSK_PROJECT, PROJECT_ROOT is the cwd repo and PROJECT_CONFIG lands
    under <cwd>/tusk/config.json — independent of TUSK_DB's location. The same
    mkdir -p must cover this case.
    """
    cwd_repo = _make_git_repo(tmp_path / "cwd")
    db_dir = tmp_path / "elsewhere"
    db_dir.mkdir()
    db_path = db_dir / "test.db"
    env = {**_clean_env(tmp_path), "TUSK_DB": str(db_path)}

    r = subprocess.run(
        [TUSK_BIN, "init"],
        cwd=str(cwd_repo),
        env=env,
        capture_output=True,
        text=True,
    )

    assert r.returncode == 0, f"tusk init failed: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert db_path.exists(), "TUSK_DB path was not created"
    assert (cwd_repo / "tusk" / "config.json").exists(), \
        "PROJECT_CONFIG was not written under cwd/tusk/"
