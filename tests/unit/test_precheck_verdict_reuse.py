"""Unit tests for tusk commit's same-HEAD precheck-verdict reuse (issue #1083).

Covers ``_reuse_precheck_verdict`` in tusk-commit.py in isolation: it must
return a bypass note only for a same-HEAD, same-test_command, pre_existing=1
verdict written within the reuse window, and return None (preserving the
exit-2 refusal) for every other case.
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_commit", os.path.join(BIN, "tusk-commit.py")
)
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


TEST_CMD = "false"  # any command; the helper never runs it


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


def _head_sha(cwd):
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return res.stdout.strip()


def _make_db(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE precheck_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                session_id INTEGER,
                head_sha TEXT NOT NULL,
                test_command TEXT NOT NULL,
                pre_existing INTEGER NOT NULL,
                exit_code INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX idx_precheck_verdicts_lookup
                ON precheck_verdicts(head_sha, test_command, id DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert(path, head_sha, test_command, pre_existing, exit_code,
            created_at=None):
    conn = sqlite3.connect(str(path))
    try:
        if created_at is None:
            conn.execute(
                "INSERT INTO precheck_verdicts "
                "(head_sha, test_command, pre_existing, exit_code) "
                "VALUES (?, ?, ?, ?)",
                (head_sha, test_command, pre_existing, exit_code),
            )
        else:
            conn.execute(
                "INSERT INTO precheck_verdicts "
                "(head_sha, test_command, pre_existing, exit_code, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (head_sha, test_command, pre_existing, exit_code, created_at),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(["init"], repo_dir)
    _git(["config", "user.email", "t@t.t"], repo_dir)
    _git(["config", "user.name", "t"], repo_dir)
    (repo_dir / "a.txt").write_text("hi\n")
    _git(["add", "a.txt"], repo_dir)
    _git(["commit", "-m", "init"], repo_dir)
    db = tmp_path / "tasks.db"
    _make_db(db)
    return str(repo_dir), str(db)


def test_reuse_returns_note_for_same_head_pre_existing(repo):
    repo_dir, db = repo
    head = _head_sha(repo_dir)
    _insert(db, head, TEST_CMD, pre_existing=1, exit_code=1)
    note = commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1)
    assert note is not None
    assert "test-precheck-bypass" in note
    assert head[:12] in note


def test_no_reuse_when_pre_existing_false(repo):
    repo_dir, db = repo
    head = _head_sha(repo_dir)
    _insert(db, head, TEST_CMD, pre_existing=0, exit_code=0)
    assert commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1) is None


def test_no_reuse_when_command_differs(repo):
    repo_dir, db = repo
    head = _head_sha(repo_dir)
    _insert(db, head, "pytest -q", pre_existing=1, exit_code=1)
    assert commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1) is None


def test_no_reuse_when_head_differs(repo):
    repo_dir, db = repo
    _insert(db, "0" * 40, TEST_CMD, pre_existing=1, exit_code=1)
    assert commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1) is None


def test_no_reuse_when_verdict_is_stale(repo):
    repo_dir, db = repo
    head = _head_sha(repo_dir)
    # 48h old — beyond the 24h reuse window.
    _insert(db, head, TEST_CMD, pre_existing=1, exit_code=1,
            created_at="2000-01-01 00:00:00")
    assert commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1) is None


def test_no_reuse_when_no_row(repo):
    repo_dir, db = repo
    assert commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1) is None


def test_no_reuse_when_db_missing(repo):
    repo_dir, _ = repo
    assert commit._reuse_precheck_verdict(
        "/nonexistent/tasks.db", repo_dir, TEST_CMD, 1
    ) is None


def test_most_recent_verdict_wins(repo):
    repo_dir, db = repo
    head = _head_sha(repo_dir)
    # Older pre_existing=1, then a newer pre_existing=0 (failure resolved) —
    # the most recent verdict must win and block reuse.
    _insert(db, head, TEST_CMD, pre_existing=1, exit_code=1,
            created_at="2099-01-01 00:00:00")
    _insert(db, head, TEST_CMD, pre_existing=0, exit_code=0)
    # The newest row by id is the pre_existing=0 one.
    assert commit._reuse_precheck_verdict(db, repo_dir, TEST_CMD, 1) is None
