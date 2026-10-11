"""Pickup contracts with a real in-memory DB and isolated workspace identities."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


class BorrowedConnection(sqlite3.Connection):
    def close(self):
        self.rollback()  # Keep the fixture's in-memory DB across invocations.
        self.set_progress_handler(None, 0)

    def dispose(self):
        super().close()


@pytest.fixture
def pickup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("codex_context_hooks", ROOT / "bin/tusk-hooks-codex.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    conn = sqlite3.connect(":memory:", factory=BorrowedConnection)
    conn.executescript('''
        CREATE TABLE tasks(id INTEGER, summary TEXT, description TEXT, status TEXT);
        CREATE TABLE task_workspaces(task_id INTEGER, workspace_path TEXT, branch TEXT);
        CREATE TABLE task_sessions(id INTEGER, task_id INTEGER, ended_at TEXT);
        CREATE TABLE acceptance_criteria(id INTEGER, task_id INTEGER, criterion TEXT, is_completed INTEGER);
        CREATE TABLE task_progress(id INTEGER, task_id INTEGER, next_steps TEXT, created_at TEXT);
        CREATE TABLE task_context_items(id INTEGER, task_id INTEGER, item_type TEXT, content TEXT, status TEXT);
        INSERT INTO tasks VALUES (42, 'Owning task', 'Restore only my context', 'In Progress'),
                                 (99, 'Other task secret', 'Unrelated data', 'In Progress');
        INSERT INTO task_sessions VALUES (43, 42, NULL), (100, 99, NULL);
        INSERT INTO acceptance_criteria VALUES (1, 42, 'unfinished promise', 0),
            (2, 42, 'already finished', 1), (3, 99, 'unrelated promise', 0);
        INSERT INTO task_progress VALUES (1, 42, 'stale instructions', '2026-01-01'),
            (2, 42, 'resume from checkpoint', '2026-01-02'),
            (3, 42, '   ', '2026-01-03'), (4, 99, 'other next steps', '2026-01-04');
        INSERT INTO task_context_items VALUES (1, 42, 'entry_point', 'bin/owned.py', 'active'),
            (2, 42, 'risk', 'retired risk', 'resolved'), (3, 99, 'decision', 'secret', 'active');
    ''')
    primary = tmp_path / 'primary'
    first, second = tmp_path / 'first', tmp_path / 'second'
    for path in (primary, first, second):
        path.mkdir()
    (primary / 'bin').mkdir()
    (primary / 'bin/tusk').touch()
    for task, path in ((42, first), (99, second)):
        conn.execute('INSERT INTO task_workspaces VALUES (?, ?, ?)',
                     (task, str(path), f'feature/TASK-{task}-test'))
    conn.commit()
    monkeypatch.chdir(first)
    monkeypatch.delenv('TUSK_CODEX_CONTEXT', raising=False)
    def git(cwd, *args):
        cwd = Path(cwd)
        if args == ('rev-parse', '--show-toplevel'):
            return str(next(p for p in (first, second, primary) if cwd == p or p in cwd.parents))
        if args == ('worktree', 'list', '--porcelain'):
            return f'worktree {primary}\n\nworktree {first}\n\nworktree {second}'
        if args == ('branch', '--show-current'):
            return f'feature/TASK-{42 if cwd == first else 99}-test'
        raise AssertionError(args)
    monkeypatch.setattr(hooks, 'git', git)
    def path_command(args, **kwargs):
        assert args == [str(primary / 'bin/tusk'), 'path']
        assert kwargs['env']['TUSK_PROJECT'] == str(primary)
        assert 'TUSK_DB' not in kwargs['env']
        return SimpleNamespace(returncode=0, stdout=str(tmp_path / 'fake.db'))
    monkeypatch.setattr(hooks.subprocess, 'run', path_command)
    def connect(database, **kwargs):
        assert database.endswith('?mode=ro') and kwargs['uri']
        return conn
    monkeypatch.setattr(hooks.sqlite3, 'connect', connect)
    def invoke(source='startup', cwd=first, **fields):
        event = {'hook_event_name': 'SessionStart', 'source': source,
                 'cwd': str(cwd), 'session_id': 'thr_codex', **fields}
        return hooks.dispatch(io.StringIO(json.dumps(event)))
    yield SimpleNamespace(hooks=hooks, conn=conn, first=first, second=second,
                          primary=primary, invoke=invoke)
    conn.dispose()


@pytest.mark.parametrize('source', ['startup', 'resume', 'clear', 'compact'])
def test_contract_1(pickup, source):
    result = pickup.invoke(source)
    assert result['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    text = result['hookSpecificOutput']['additionalContext']
    for expected in ('TASK-42', 'Owning task', 'Restore only my context', 'unfinished promise',
                     'resume from checkpoint', 'bin/owned.py', 'tusk task-brief 42'):
        assert expected in text
    for excluded in ('already finished', 'stale instructions', 'retired risk', 'Other task', 'secret'):
        assert excluded not in text
    assert len(text.encode('utf-8')) <= 6000
    assert 'continue' not in result and 'decision' not in result


def test_contract_2(pickup, monkeypatch):
    p = pickup
    nested = p.first / 'nested'
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv('TUSK_DB', '/wrong/project.db')
    monkeypatch.setenv('TUSK_PROJECT', str(p.second))
    assert 'TASK-42' in p.invoke(cwd=nested)['hookSpecificOutput']['additionalContext']
    assert p.invoke(cwd=p.second) == {}  # Mismatched payload/caller workspace.
    monkeypatch.chdir(p.second)
    text = p.invoke(cwd=p.second)['hookSpecificOutput']['additionalContext']
    assert 'TASK-99' in text and 'unfinished promise' not in text
    monkeypatch.chdir(p.primary)
    assert p.invoke(cwd=p.primary) == {}  # Even if there is only one global session.
    p.conn.execute('DELETE FROM task_sessions WHERE task_id = 99')
    p.conn.commit()
    assert p.invoke(cwd=p.primary) == {}
    monkeypatch.chdir(p.first)
    p.conn.execute('INSERT INTO task_sessions VALUES (44, 42, NULL)')
    p.conn.commit()
    assert p.invoke() == {}
    p.conn.execute('DELETE FROM task_sessions WHERE id=44')
    p.conn.execute("UPDATE tasks SET status='Done' WHERE id=42")
    p.conn.commit()
    assert p.invoke() == {}  # Stale open session cannot revive a closed task.
    p.conn.execute("UPDATE tasks SET status='In Progress' WHERE id=42")
    p.conn.execute('DELETE FROM task_sessions WHERE task_id=42')
    p.conn.commit()
    assert p.invoke() == {}


def test_contract_3(pickup, monkeypatch, capsys):
    p = pickup
    before = list(p.conn.iterdump())
    original = p.invoke()
    assert p.invoke() == original and p.invoke('compact') == original
    assert list(p.conn.iterdump()) == before  # No checkpoints or dedup writes.
    huge = '🦣' * 10000
    p.conn.execute('UPDATE tasks SET description=?, summary=? WHERE id=42', (huge, huge))
    p.conn.execute('UPDATE task_progress SET next_steps=? WHERE task_id=42', (huge,))
    p.conn.executemany('INSERT INTO acceptance_criteria VALUES (?, 42, ?, 0)',
                       [(n, huge) for n in range(10, 40)])
    p.conn.commit()
    text = p.invoke()['hookSpecificOutput']['additionalContext']
    assert len(text.encode('utf-8')) <= 6000
    assert 'Latest next steps:' in text and 'Unfinished criteria' in text
    assert 'More unfinished criteria omitted' in text and text.endswith('tusk task-brief 42')
    assert p.invoke(source='unknown') == {} and p.invoke(source=[]) == {}
    assert p.invoke(hook_event_name='PostCompact') == {}  # Do not double-inject.
    p.conn.execute('DROP TABLE task_progress')
    p.conn.commit()
    assert p.invoke() == {}  # An unavailable schema is advisory, too.
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('git', 1)
    monkeypatch.setattr(p.hooks, 'resolve_context', fail)
    assert p.invoke() == {}  # Timeout is advisory and does not stop the session.
    def unexpected(*args, **kwargs):
        pytest.fail('opt-out must avoid identity and database reads')
    monkeypatch.setattr(p.hooks, 'resolve_context', unexpected)
    for value in ('0', 'false', 'off'):
        monkeypatch.setenv('TUSK_CODEX_CONTEXT', value)
        assert p.invoke() == {}
    assert capsys.readouterr() == ('', '')
