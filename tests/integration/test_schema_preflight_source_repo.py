"""Integration coverage for the source-repo vs consumer schema-mismatch advisory
(TASK-498, issue #934).

When tusk/tasks.db is on a newer user_version than bin/tusk-migrate.py's
MIGRATIONS registry knows about, bin/tusk's preflight emits a 'Schema mismatch'
advisory. The recommended action depends on whether bin/tusk is running from
the tusk source repo (origin matches canonical github.com/gioe/tusk URLs):

  - source repo  -> "Run 'git pull' (or 'tusk sync-main' ...)"
  - consumer     -> "Run 'tusk upgrade' to update."

These tests stand up a copy of the source repo's bin/ in a tempdir, set
different origin URL shapes on its .git, stamp the db_path fixture's DB past
supported_max, and assert the correct wording fires from the tempdir bin/tusk.
"""

import importlib.util
import os
import shutil
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_BIN_DIR = os.path.join(REPO_ROOT, "bin")
SRC_CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate", os.path.join(SRC_BIN_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


def _supported_schema_max():
    return max(v for v, _ in tusk_migrate.MIGRATIONS)


@pytest.fixture
def fake_install(tmp_path):
    """Copy bin/ + config.default.json into a tempdir so bin/tusk's INSTALL_DIR
    can be manipulated independently of the real source repo. Callers add a
    .git/ directory with the desired origin via _init_git()."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    shutil.copytree(SRC_BIN_DIR, install_dir / "bin")
    shutil.copy(SRC_CONFIG_DEFAULT, install_dir / "config.default.json")
    return install_dir


def _init_git(install_dir, origin_url=None):
    subprocess.run(
        ["git", "init", "-q"], cwd=str(install_dir), check=True, encoding="utf-8",
    )
    if origin_url is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", origin_url],
            cwd=str(install_dir), check=True, encoding="utf-8",
        )


def _stamp_user_version(db_file, version):
    conn = sqlite3.connect(str(db_file))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def _run(install_dir, db_file):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_file)
    env["TUSK_QUIET"] = "1"  # silence unrelated source-repo-stale warning
    return subprocess.run(
        [str(install_dir / "bin" / "tusk"), "task-list"],
        capture_output=True, text=True, env=env, encoding="utf-8",
    )


CANONICAL_ORIGINS = [
    "https://github.com/gioe/tusk",
    "https://github.com/gioe/tusk.git",
    "git@github.com:gioe/tusk",
    "git@github.com:gioe/tusk.git",
    "ssh://git@github.com/gioe/tusk",
    "ssh://git@github.com/gioe/tusk.git",
]


@pytest.mark.parametrize("origin", CANONICAL_ORIGINS)
def test_canonical_origin_emits_source_repo_wording(db_path, fake_install, origin):
    """Every canonical github.com/gioe/tusk URL variant triggers the source-repo
    advisory recommending git pull / tusk sync-main."""
    _init_git(fake_install, origin)
    _stamp_user_version(db_path, _supported_schema_max() + 1)

    result = _run(fake_install, db_path)

    assert result.returncode != 0
    assert "Schema mismatch" in result.stderr
    assert "git pull" in result.stderr
    assert "tusk sync-main" in result.stderr
    # Consumer wording must NOT appear in the source-repo branch.
    assert "Run 'tusk upgrade'" not in result.stderr


@pytest.mark.parametrize("origin", [
    "https://github.com/someone-else/tusk-fork.git",
    "git@github.com:someone-else/tusk-fork.git",
    "https://gitlab.com/gioe/tusk.git",
])
def test_non_canonical_origin_emits_consumer_wording(db_path, fake_install, origin):
    """Forks and non-canonical hosts fall through to the consumer wording."""
    _init_git(fake_install, origin)
    _stamp_user_version(db_path, _supported_schema_max() + 1)

    result = _run(fake_install, db_path)

    assert result.returncode != 0
    assert "Schema mismatch" in result.stderr
    assert "Run 'tusk upgrade'" in result.stderr
    # Source-repo wording must NOT leak into the consumer branch.
    assert "git pull" not in result.stderr
    assert "tusk sync-main" not in result.stderr


def test_no_origin_emits_consumer_wording(db_path, fake_install):
    """A git repo with no origin configured falls through to consumer wording."""
    _init_git(fake_install, origin_url=None)
    _stamp_user_version(db_path, _supported_schema_max() + 1)

    result = _run(fake_install, db_path)

    assert result.returncode != 0
    assert "Run 'tusk upgrade'" in result.stderr
    assert "git pull" not in result.stderr


def test_no_git_repo_emits_consumer_wording(db_path, fake_install):
    """No .git directory at INSTALL_DIR (the canonical consumer install shape)
    falls through to consumer wording."""
    # Deliberately skip _init_git -- no .git directory.
    _stamp_user_version(db_path, _supported_schema_max() + 1)

    result = _run(fake_install, db_path)

    assert result.returncode != 0
    assert "Run 'tusk upgrade'" in result.stderr
    assert "git pull" not in result.stderr


@pytest.mark.parametrize("origin,branch", [
    ("https://github.com/gioe/tusk.git", "source"),
    ("https://github.com/someone-else/tusk-fork.git", "consumer"),
])
def test_both_branches_name_both_schema_versions(db_path, fake_install, origin, branch):
    """Acceptance criterion 2319: both wordings must name v<user_version> and
    v<supported_max> so the operator can correlate against the registry."""
    supported = _supported_schema_max()
    _init_git(fake_install, origin)
    _stamp_user_version(db_path, supported + 1)

    result = _run(fake_install, db_path)

    assert f"v{supported + 1}" in result.stderr, branch
    assert f"<=v{supported}" in result.stderr, branch
