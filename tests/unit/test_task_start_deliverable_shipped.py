"""Unit tests for task-start's shipped-commit deliverable signal (issue #948).

task-start's deliverable_check_needed used to be true only when a criterion was
already completed. An orphaned task whose [TASK-N] commits already shipped to the
default branch (or origin/<default> via a no-checkout fast-forward push) with zero
criteria done escaped the deliverable check. _task_commits_on_default supplies the
stronger orphaned-work signal so check-deliverables still runs.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-start.py")


def _load():
    spec = importlib.util.spec_from_file_location("tusk_task_start", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_repo(path):
    os.makedirs(path)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "t")


def _commit(path, name, content, message):
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        fh.write(content)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", message)


def _db_path(repo):
    # Path need not exist; the helper derives repo_root from it only as a
    # fallback, and these tests pin TUSK_REPO_ROOT directly.
    return os.path.join(repo, "tusk", "tasks.db")


def test_commit_on_local_default_detected(tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "f.txt", "x", "[TASK-42] ship work")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)

    assert mod._task_commits_on_default(_db_path(repo), 42) is True


def test_no_task_commit_returns_false(tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "f.txt", "x", "unrelated base work")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)

    assert mod._task_commits_on_default(_db_path(repo), 42) is False


def test_other_task_commit_does_not_match(tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "f.txt", "x", "[TASK-99] different task")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)

    assert mod._task_commits_on_default(_db_path(repo), 42) is False


def test_commit_only_on_origin_default_detected(tmp_path, monkeypatch):
    """No-checkout fast-forward push: origin/main has the commit but local main
    is behind. The origin/<default> arm of the scan must still detect it."""
    remote = str(tmp_path / "remote.git")
    _git(str(tmp_path), "init", "--bare", "-q", "remote.git")

    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "f.txt", "x", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", remote)
    _git(repo, "push", "-q", "origin", "main")

    # Ship a [TASK-77] commit to origin, then rewind local main behind origin.
    _commit(repo, "g.txt", "y", "[TASK-77] shipped via no-checkout ff")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "reset", "--hard", "HEAD~1")
    _git(repo, "remote", "set-head", "origin", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)

    # Local main no longer carries the commit, but origin/main does.
    assert mod._task_commits_on_default(_db_path(repo), 77) is True


def _completed(returncode=0, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_default_branch_staleness_warning_when_local_default_behind(monkeypatch):
    calls = []
    monkeypatch.setattr(mod._git_helpers, "default_branch", lambda repo: "main")

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:4] == ["git", "-C", "/repo", "fetch"]:
            return _completed()
        if args[:4] == ["git", "-C", "/repo", "rev-list"]:
            return _completed(stdout="3\n")
        return _completed(returncode=1, stderr="unexpected")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    warning = mod._default_branch_staleness_warning("/repo")

    assert warning == {
        "type": "stale_default_branch",
        "default_branch": "main",
        "behind_count": 3,
        "message": (
            "local main is 3 commit(s) behind origin/main; "
            "consider syncing before investigating"
        ),
    }
    assert calls == [
        ["git", "-C", "/repo", "fetch", "origin", "main", "--quiet"],
        ["git", "-C", "/repo", "rev-list", "--count", "main..origin/main"],
    ]


def test_default_branch_staleness_warning_silent_when_current(monkeypatch):
    monkeypatch.setattr(mod._git_helpers, "default_branch", lambda repo: "main")

    def fake_run(args, **kwargs):
        if args[:4] == ["git", "-C", "/repo", "rev-list"]:
            return _completed(stdout="0\n")
        return _completed()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._default_branch_staleness_warning("/repo") is None


def test_default_branch_staleness_warning_silent_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(mod._git_helpers, "default_branch", lambda repo: "main")

    def fake_run(args, **kwargs):
        if args[:4] == ["git", "-C", "/repo", "fetch"]:
            return _completed(returncode=128, stderr="network unavailable")
        if args[:4] == ["git", "-C", "/repo", "rev-list"]:
            return _completed(returncode=128, stderr="bad ref")
        return _completed()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._default_branch_staleness_warning("/repo") is None
