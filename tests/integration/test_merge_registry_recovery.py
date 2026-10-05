"""Registry write contention after worktree removal and safe merge replay (#1282)."""
import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('merge_registry', ROOT / 'bin/tusk-merge.py')
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.com')
    (root / '.gitignore').write_text('tusk/\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'initial')
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', '-qu', 'origin', 'main')
    workspace = tmp_path / 'workspace'
    branch = 'feature/TASK-1-registry'
    git(root, 'worktree', 'add', '-qb', branch, str(workspace))
    (workspace / 'change.txt').write_text('task change\n')
    git(workspace, 'add', '.')
    git(workspace, 'commit', '-qm', '[TASK-1] change')
    (root / 'tusk').mkdir()
    db = root / 'tusk/tasks.db'
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE tasks(id INTEGER PRIMARY KEY, status TEXT, closed_reason TEXT);
            INSERT INTO tasks VALUES(1,'Done','completed');
            CREATE TABLE task_sessions(id INTEGER PRIMARY KEY,task_id INTEGER,started_at TEXT,ended_at TEXT);
            INSERT INTO task_sessions VALUES(1,1,'2026-01-01','2026-01-02');
            CREATE TABLE task_workspaces(id INTEGER PRIMARY KEY,task_id INTEGER,branch TEXT,workspace_path TEXT,created_at TEXT);
        """)
        conn.execute('INSERT INTO task_workspaces VALUES(1,1,?,?,?)', (branch,str(workspace),'2026-01-01'))
    config = root / 'tusk/config.json'
    config.write_text('{}')
    monkeypatch.chdir(root)
    monkeypatch.setenv('TUSK_BUSY_TIMEOUT_MS','1')
    monkeypatch.setenv('TUSK_WRITE_RETRIES','1')
    monkeypatch.setenv('TUSK_WRITE_RETRY_BASE_MS','1')
    return root, db, config, workspace, branch


def test_exhausted_registry_lock_is_partial_cleanup(repo, capsys):
    root, db, config, workspace, branch = repo
    with sqlite3.connect(db) as lock:
        lock.execute('BEGIN IMMEDIATE')
        assert merge._cleanup_no_checkout_workspace(str(db), 1, branch) is False
    assert not workspace.exists()
    assert git(root,'rev-parse','--verify',branch)
    assert 'registry' in capsys.readouterr().err.lower()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM task_workspaces').fetchone()[0] == 1


def test_transient_registry_lock_retries_fresh_connection(repo, monkeypatch):
    root, db, config, workspace, branch = repo
    lock = sqlite3.connect(db)
    lock.execute('BEGIN IMMEDIATE')
    sleeps = []
    connections = []
    original_connection = merge._db_lib.get_connection
    def track_connection(path):
        conn = original_connection(path)
        connections.append(conn)
        return conn
    monkeypatch.setattr(merge._db_lib, "get_connection", track_connection)
    def release_lock(delay):
        sleeps.append(delay)
        lock.rollback()
    monkeypatch.setattr(merge._db_lib.time, 'sleep', release_lock)
    try:
        assert merge._cleanup_no_checkout_workspace(str(db), 1, branch) is True
    finally:
        lock.close()
    assert len(sleeps) == 1
    assert len(connections) == 2 and connections[0] is not connections[1]
    assert not workspace.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM task_workspaces').fetchone()[0] == 0


def test_non_lock_registry_error_is_not_hidden(repo, monkeypatch):
    def corrupt(*args, **kwargs):
        raise sqlite3.OperationalError('no such table: task_workspaces')
    monkeypatch.setattr(merge, '_forget_task_workspace', corrupt)
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        merge._try_forget_task_workspace(str(repo[1]), 1, 1)


def replay(repo, monkeypatch, *, fail_fetch=False):
    root, db, config, workspace, branch = repo
    original_run = merge.run
    commands = []
    def guarded_run(args, check=True):
        commands.append(args)
        assert args[:2] not in (['git','checkout'], ['git','push'], ['git','rebase'], ['git','stash'])
        if fail_fetch and args[:2] == ['git','fetch']:
            return subprocess.CompletedProcess(args, 1, '', 'unreachable')
        return original_run(args, check=check)
    monkeypatch.setattr(merge, 'run', guarded_run)
    monkeypatch.setattr(merge, 'detect_default_branch', lambda: 'main')
    rc = merge.main([str(db), str(config), '1', '--session', '1'])
    return rc, commands


@pytest.mark.parametrize('current_default', [False, True])
def test_main_recovers_published_missing_workspace_without_touching_primary(repo, monkeypatch, current_default):
    root, db, config, workspace, branch = repo
    git(root, 'push', '-q', 'origin', f'{branch}:main')
    if current_default:
        git(root, 'merge', '--ff-only', branch)
    git(root, 'worktree', 'remove', str(workspace))
    before = git(root, 'rev-parse', 'HEAD')
    (root / '.gitignore').write_text('tusk/\nlocal dirty change\n')
    rc, commands = replay(repo, monkeypatch)
    assert rc == 0
    assert git(root, 'rev-parse', 'HEAD') == before
    assert (root / '.gitignore').read_text().endswith('local dirty change\n')
    assert any(cmd[:2] == ['git','fetch'] for cmd in commands)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM task_workspaces').fetchone()[0] == 0
    assert subprocess.run(['git','-C',str(root),'show-ref','--verify',f'refs/heads/{branch}'], capture_output=True).returncode != 0


def test_main_retry_stays_partial_while_registry_locked_then_recovers(repo, monkeypatch):
    root, db, config, workspace, branch = repo
    git(root, 'push', '-q', 'origin', f'{branch}:main')
    with sqlite3.connect(db) as lock:
        lock.execute('BEGIN IMMEDIATE')
        assert merge._cleanup_no_checkout_workspace(str(db), 1, branch) is False
        rc, _ = replay(repo, monkeypatch)
        assert rc == 3
        assert git(root, 'rev-parse', '--verify', branch)
    # Reuse the guarded runner to ensure recovery still avoids primary mutations.
    assert merge.main([str(db), str(config), '1', '--session', '1']) == 0


@pytest.mark.parametrize('reason', ['unpublished', 'fetch_failed', 'remote_ref_deleted', 'not_completed'])
def test_main_refuses_unproven_missing_workspace(repo, monkeypatch, reason):
    root, db, config, workspace, branch = repo
    if reason != 'unpublished':
        git(root, 'push', '-q', 'origin', f'{branch}:main')
    if reason == 'remote_ref_deleted':
        remote = git(root, 'remote', 'get-url', 'origin')
        git(remote, 'update-ref', '-d', 'refs/heads/main')
    if reason == 'not_completed':
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE tasks SET status='In Progress',closed_reason=NULL")
    git(root, 'worktree', 'remove', str(workspace))
    rc, commands = replay(repo, monkeypatch, fail_fetch=reason == 'fetch_failed')
    assert rc == 2
    assert not any(cmd[:3] == ['git','branch','-D'] for cmd in commands)
    assert git(root, 'rev-parse', '--verify', branch)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM task_workspaces').fetchone()[0] == 1


def test_stale_sibling_registry_lock_preserves_branch(repo):
    root, db, config, workspace, branch = repo
    git(root, 'push', '-q', 'origin', f'{branch}:main')
    sibling = 'feature/TASK-1-sibling'
    git(root, 'branch', sibling, branch)
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO task_workspaces VALUES(2,1,?,?,?)',
                     (sibling, str(root / 'missing-sibling'), '2026-01-02'))
    with sqlite3.connect(db) as lock:
        lock.execute('BEGIN IMMEDIATE')
        assert merge._reconcile_duplicate_task_workspaces(str(db), 1, branch, 'main') is False
    assert git(root, 'rev-parse', '--verify', sibling)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM task_workspaces').fetchone()[0] == 2
