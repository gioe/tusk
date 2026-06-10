"""Unit tests for task-start's shipped-commit deliverable signal (issue #948).

task-start's deliverable_check_needed used to be true only when a criterion was
already completed. An orphaned task whose [TASK-N] commits already shipped to the
default branch (or origin/<default> via a no-checkout fast-forward push) with zero
criteria done escaped the deliverable check. _task_commits_on_default supplies the
stronger orphaned-work signal so check-deliverables still runs.
"""

import importlib.util
import os
import sqlite3
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


# ── Prefix-collision file-overlap heuristic (issue #1056) ─────────────
#
# A bare [TASK-<id>] message match from a prior task-numbering epoch must
# not flip deliverable_check_needed when the task has a scope signal the
# matched commits don't touch. Minimal-schema fixture: only the columns
# task_referenced_paths / task_referenced_basenames query — intentionally
# NOT a mirror of bin/tusk's full schema.


def _scope_conn(task_id=42, summary="", description=""):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, summary TEXT, description TEXT)"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria ("
        "id INTEGER PRIMARY KEY, task_id INTEGER, criterion TEXT, verification_spec TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
        (task_id, summary, description),
    )
    conn.commit()
    return conn


def _commit_at(path, relname, content, message):
    full = os.path.join(path, relname)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", message)


def test_prior_epoch_prefix_match_with_scope_signal_not_flagged(tmp_path, monkeypatch):
    """Issue #1056: the prior-epoch commit touches only unrelated.txt while the
    task's scope signal names bin/tusk-task-start.py — prefix-match false
    positive, must not force deliverable_check_needed."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "unrelated.txt", "x", "[TASK-42] prior-epoch commit touching only unrelated.txt")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)
    conn = _scope_conn(description="Fix the helper in bin/tusk-task-start.py")

    assert mod._task_commits_on_default(_db_path(repo), 42, conn) is False


def test_prior_epoch_prefix_match_no_scope_signal_stays_true(tmp_path, monkeypatch):
    """No referenced paths and no bare basenames = nothing to discriminate
    with — keep the conservative True so genuine orphaned work isn't dropped."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "unrelated.txt", "x", "[TASK-42] prior-epoch commit touching only unrelated.txt")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)
    conn = _scope_conn(description="no path-shaped tokens here")

    assert mod._task_commits_on_default(_db_path(repo), 42, conn) is True


def test_genuine_overlap_full_path_still_flagged(tmp_path, monkeypatch):
    """A matched commit touching a file the task references by full path is a
    real orphaned-work signal — the heuristic must keep it."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit_at(repo, "bin/helper.py", "x", "[TASK-42] ship the helper")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)
    conn = _scope_conn(description="Fix the logic in bin/helper.py")

    assert mod._task_commits_on_default(_db_path(repo), 42, conn) is True


def test_genuine_overlap_bare_basename_still_flagged(tmp_path, monkeypatch):
    """A task that names a touched file by bare basename (no directory prefix)
    still counts as overlap via the basename leg (issue #670)."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit_at(repo, "skills/retro/FULL-RETRO.md", "x", "[TASK-42] update retro doc")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)
    conn = _scope_conn(description="Rewrite FULL-RETRO.md guidance")

    assert mod._task_commits_on_default(_db_path(repo), 42, conn) is True


def test_no_conn_keeps_conservative_true(tmp_path, monkeypatch):
    """conn=None (legacy 2-arg call shape) cannot resolve a scope signal —
    matched commits keep the conservative True."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    _commit(repo, "unrelated.txt", "x", "[TASK-42] prior-epoch commit")
    _git(repo, "branch", "-M", "main")
    monkeypatch.setenv("TUSK_REPO_ROOT", repo)

    assert mod._task_commits_on_default(_db_path(repo), 42) is True


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
