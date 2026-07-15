"""Regression coverage for task-scoped finalization across diverged ancestry."""

import importlib.util
import io
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_merge():
    spec = importlib.util.spec_from_file_location(
        "tusk_merge_task_published_diverged", REPO_ROOT / "bin" / "tusk-merge.py"
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
        encoding="utf-8",
    ).stdout.strip()


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _build_topology(tmp_path: Path, *, add_unpreserved_passenger: bool = False):
    remote = tmp_path / "origin.git"
    primary = tmp_path / "primary"
    peer = tmp_path / "peer"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(primary)], check=True)
    _git(primary, "config", "user.email", "test@example.com")
    _git(primary, "config", "user.name", "Test User")
    _write(primary / "seed.txt", "seed\n")
    _git(primary, "add", "seed.txt")
    _git(primary, "commit", "-m", "seed")
    _git(primary, "remote", "add", "origin", str(remote))
    _git(primary, "push", "-u", "origin", "main")

    _write(primary / "local-only.txt", "preserve locally\n")
    _git(primary, "add", "local-only.txt")
    _git(primary, "commit", "-m", "Unpublished local main")
    local_main_sha = _git(primary, "rev-parse", "main")

    branch = "feature/TASK-803-published"
    _git(primary, "checkout", "-b", branch)
    _write(primary / "task.txt", "task patch\n")
    _git(primary, "add", "task.txt")
    _git(primary, "commit", "-m", "[TASK-803] task patch")
    if add_unpreserved_passenger:
        _write(primary / "passenger.txt", "not preserved\n")
        _git(primary, "add", "passenger.txt")
        _git(primary, "commit", "-m", "Unpreserved branch passenger")

    subprocess.run(["git", "clone", str(remote), str(peer)], check=True)
    _git(peer, "config", "user.email", "test@example.com")
    _git(peer, "config", "user.name", "Test User")
    _write(peer / "task.txt", "task patch\n")
    _git(peer, "add", "task.txt")
    _git(peer, "commit", "-m", "[TASK-803] independently published")
    _git(peer, "push", "origin", "main")
    _git(primary, "fetch", "origin")
    return primary, remote, branch, local_main_sha


def test_publication_proof_accepts_task_patch_and_preserved_local_ancestry(
    tmp_path, monkeypatch
):
    primary, _remote, branch, _local_main_sha = _build_topology(tmp_path)
    merge = _load_merge()
    monkeypatch.chdir(primary)

    assert merge._task_scope_already_published(branch, 803, "main") is True


def test_publication_proof_rejects_unpreserved_branch_passenger(tmp_path, monkeypatch):
    primary, _remote, branch, _local_main_sha = _build_topology(
        tmp_path, add_unpreserved_passenger=True
    )
    merge = _load_merge()
    monkeypatch.chdir(primary)

    assert merge._task_scope_already_published(branch, 803, "main") is False


def test_no_checkout_finalizes_without_pushing_or_touching_local_main(
    tmp_path, monkeypatch
):
    primary, remote, branch, local_main_sha = _build_topology(tmp_path)
    merge = _load_merge()
    monkeypatch.chdir(primary)
    remote_before = _git(remote, "rev-parse", "refs/heads/main")
    calls = []
    original_run = merge.run

    def recording_run(args, check=True):
        calls.append(list(args))
        return original_run(args, check=check)

    monkeypatch.setattr(merge, "run", recording_run)
    monkeypatch.setattr(merge, "checkpoint_wal", lambda _db: None)
    monkeypatch.setattr(merge, "_delete_remote_feature_branch_if_tracking", lambda _b: None)
    monkeypatch.setattr(merge, "_warn_branch_auto_stash", lambda _tid: None)
    close_args = {}

    def close_task(*_args, **kwargs):
        close_args.update(kwargs)
        return 0

    monkeypatch.setattr(merge, "_close_completed_task", close_task)
    monkeypatch.setattr(merge, "_cleanup_no_checkout_workspace", lambda *_a: True)
    monkeypatch.setattr(
        merge, "_reconcile_duplicate_task_workspaces", lambda *_a: True
    )

    def unexpected_refresh(*_args, **_kwargs):
        raise AssertionError("published-task finalization must not sync primary")

    monkeypatch.setattr(merge, "_maybe_refresh_deployed_bin", unexpected_refresh)
    monkeypatch.setattr(merge, "_maybe_advise_stale_deployed_bin", unexpected_refresh)

    stderr = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
        rc = merge._complete_no_checkout_fast_forward(
            branch_name=branch,
            default_branch="main",
            task_id=803,
            session_id=804,
            tusk_bin="/usr/bin/true",
            db_path=str(primary / "tusk" / "tasks.db"),
            session_was_closed=True,
            did_stash=False,
            use_rebase=False,
        )

    assert rc == 0
    assert _git(primary, "rev-parse", "main") == local_main_sha
    assert _git(remote, "rev-parse", "refs/heads/main") == remote_before
    assert not [
        call
        for call in calls
        if call[:2] == ["git", "push"] and any(arg.endswith(":main") for arg in call)
    ]
    assert close_args["merge_commit_sha"] is None
    assert close_args["merge_base_sha"] is None
    assert "without publishing unrelated local-default work" in stderr.getvalue()
