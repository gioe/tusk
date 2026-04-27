"""Regression: tusk init must respect TUSK_PROJECT for auxiliary file edits.

Reproduces issue #595: when TUSK_PROJECT=<path> is set, tusk init correctly
reroutes the database but used to mutate the cwd source repo's .gitignore and
CLAUDE.md/AGENTS.md. The pin must fully isolate every side effect — DB,
config, gitignore, and agent-doc updates all land under TUSK_PROJECT.
"""

import hashlib
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


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


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
    (source / ".gitignore").write_text("")
    (source / "CLAUDE.md").write_text("")
    return source, target, _clean_env(tmp_path)


def test_tusk_project_does_not_mutate_source_aux_files(source_and_target):
    source, target, env = source_and_target
    src_gitignore_before = _md5(source / ".gitignore")
    src_claude_before = _md5(source / "CLAUDE.md")

    env_pinned = {**env, "TUSK_PROJECT": str(target)}
    r = subprocess.run(
        [TUSK_BIN, "init"],
        cwd=str(source),
        env=env_pinned,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"tusk init failed: {r.stderr}"

    assert _md5(source / ".gitignore") == src_gitignore_before, \
        "source .gitignore was mutated despite TUSK_PROJECT pin"
    assert _md5(source / "CLAUDE.md") == src_claude_before, \
        "source CLAUDE.md was mutated despite TUSK_PROJECT pin"


def test_tusk_project_writes_aux_files_under_pin(source_and_target):
    source, target, env = source_and_target

    env_pinned = {**env, "TUSK_PROJECT": str(target)}
    r = subprocess.run(
        [TUSK_BIN, "init"],
        cwd=str(source),
        env=env_pinned,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr

    target_gitignore = target / ".gitignore"
    target_claude = target / "CLAUDE.md"
    assert target_gitignore.exists(), "TUSK_PROJECT/.gitignore was not created"
    assert "# tusk install files" in target_gitignore.read_text()
    assert "tusk/tasks.db" in target_gitignore.read_text()

    assert target_claude.exists(), "TUSK_PROJECT/CLAUDE.md was not created"
    assert "<!-- tusk-task-tools -->" in target_claude.read_text()


def test_no_pin_writes_aux_files_in_cwd_repo(tmp_path):
    """No regression: without TUSK_PROJECT, aux edits land in the cwd repo."""
    cwd_repo = _make_git_repo(tmp_path / "cwd")
    env = _clean_env(tmp_path)

    r = subprocess.run(
        [TUSK_BIN, "init"],
        cwd=str(cwd_repo),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr

    cwd_gitignore = cwd_repo / ".gitignore"
    cwd_claude = cwd_repo / "CLAUDE.md"
    assert cwd_gitignore.exists()
    assert "# tusk install files" in cwd_gitignore.read_text()
    assert cwd_claude.exists()
    assert "<!-- tusk-task-tools -->" in cwd_claude.read_text()
