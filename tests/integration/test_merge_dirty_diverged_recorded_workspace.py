"""Regression coverage for dirty, diverged recorded-worktree merge routing."""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from tests.integration.conftest import _insert_session, _insert_task


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_merge_module():
    spec = importlib.util.spec_from_file_location(
        "tusk_merge_dirty_diverged",
        REPO_ROOT / "bin" / "tusk-merge.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _snapshot(primary: Path) -> dict[str, str]:
    return {
        "head": _git(primary, "rev-parse", "HEAD"),
        "unstaged": _git(primary, "diff", "--binary"),
        "staged": _git(primary, "diff", "--cached", "--binary"),
        "unmerged": _git(primary, "ls-files", "-u"),
        "status": _git(primary, "status", "--short"),
        "stashes": _git(primary, "stash", "list", "--format=%H"),
    }


@pytest.mark.parametrize(
    "local_default_commit",
    [True, False],
    ids=["two-sided-default-divergence", "behind-only-default"],
)
def test_dirty_primary_routes_before_stash_and_remains_unchanged(
    db_path, config_path, monkeypatch, tmp_path, local_default_commit
):
    remote = tmp_path / "origin.git"
    primary = tmp_path / "primary"
    peer = tmp_path / "peer"
    workspace = tmp_path / "task-worktree"

    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(primary)], check=True)
    _git(primary, "config", "user.email", "test@example.com")
    _git(primary, "config", "user.name", "Test User")
    _write(primary / "seed.txt", "seed\n")
    _write(primary / "dirty.txt", "clean\n")
    _write(primary / "staged.txt", "clean\n")
    _git(primary, "add", ".")
    _git(primary, "commit", "-m", "seed")
    _git(primary, "remote", "add", "origin", str(remote))
    _git(primary, "push", "-u", "origin", "main")

    conn = sqlite3.connect(str(db_path))
    try:
        task_id = _insert_task(conn)
        session_id = _insert_session(conn, task_id)
    finally:
        conn.close()

    branch = f"feature/TASK-{task_id}-recorded"
    _git(primary, "worktree", "add", "-b", branch, str(workspace), "origin/main")
    _write(workspace / "feature.txt", "feature\n")
    _git(workspace, "add", "feature.txt")
    _git(workspace, "commit", "-m", f"[TASK-{task_id}] feature")

    subprocess.run(["git", "clone", str(remote), str(peer)], check=True)
    _git(peer, "config", "user.email", "test@example.com")
    _git(peer, "config", "user.name", "Test User")
    _write(peer / "remote.txt", "remote\n")
    _git(peer, "add", "remote.txt")
    _git(peer, "commit", "-m", "remote advance")
    _git(peer, "push", "origin", "main")

    if local_default_commit:
        _write(primary / "local.txt", "local\n")
        _git(primary, "add", "local.txt")
        _git(primary, "commit", "-m", "local advance")
    _git(primary, "fetch", "origin", "main")

    tusk_merge = _load_merge_module()
    previous_cwd = os.getcwd()
    try:
        os.chdir(primary)
        assert tusk_merge._default_checkout_has_tracked_changes() is False
    finally:
        os.chdir(previous_cwd)

    _write(primary / "dirty.txt", "unstaged user work\n")
    _write(primary / "staged.txt", "staged user work\n")
    _git(primary, "add", "staged.txt")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO task_workspaces (task_id, branch, workspace_path) "
            "VALUES (?, ?, ?)",
            (task_id, branch, str(workspace)),
        )
        conn.commit()
    finally:
        conn.close()

    before = _snapshot(primary)
    routed = {}
    monkeypatch.setattr(tusk_merge, "detect_default_branch", lambda: "main")
    monkeypatch.setattr(tusk_merge, "_guard_no_open_completion_criteria", lambda *_: 0)
    monkeypatch.setattr(tusk_merge, "_run_pre_merge_lint", lambda *_, **__: 0)
    monkeypatch.setattr(tusk_merge, "_emit_spec_drift_advisory", lambda *_, **__: None)
    monkeypatch.setattr(
        tusk_merge,
        "_resolve_stable_tusk_bin",
        lambda *_: str(REPO_ROOT / "bin" / "tusk"),
    )

    def _capture_no_checkout(**kwargs):
        routed.update(kwargs)
        routed["cwd"] = os.path.realpath(os.getcwd())
        return 2

    monkeypatch.setattr(
        tusk_merge,
        "_complete_no_checkout_fast_forward",
        _capture_no_checkout,
    )

    stderr = io.StringIO()
    try:
        os.chdir(primary)
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = tusk_merge.main(
                [
                    str(db_path),
                    str(config_path),
                    str(task_id),
                    "--session",
                    str(session_id),
                ]
            )
    finally:
        os.chdir(previous_cwd)

    assert rc == 2
    assert routed["cwd"] == os.path.realpath(workspace)
    assert routed["did_stash"] is False
    assert _snapshot(primary) == before
    assert "checkout has tracked changes" in stderr.getvalue()
    assert "no-checkout safety path" in stderr.getvalue()
    assert "Stashing uncommitted changes" not in stderr.getvalue()
