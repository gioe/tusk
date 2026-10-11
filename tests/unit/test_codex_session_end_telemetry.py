"""Bounded Codex hook ingestion reuses real transcript/pricing parsers."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_') + '_hook_test', ROOT / 'bin' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def telemetry(tmp_path):
    stats = load('tusk-session-stats')
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE tasks(id INTEGER, status TEXT);
        INSERT INTO tasks VALUES(42, 'In Progress');
        CREATE TABLE task_sessions(
            id INTEGER PRIMARY KEY, task_id INTEGER, started_at TEXT, ended_at TEXT,
            transcript_path TEXT, transcript_provider TEXT, tokens_in INTEGER, tokens_out INTEGER,
            cost_dollars REAL, model TEXT, request_count INTEGER, peak_context_tokens INTEGER,
            first_context_tokens INTEGER, last_context_tokens INTEGER, context_window INTEGER,
            cache_read_tokens_in INTEGER, cache_write_tokens_in INTEGER, uncached_tokens_in INTEGER,
            telemetry_status TEXT, active_seconds INTEGER);
        INSERT INTO task_sessions(id, task_id, started_at) VALUES(43, 42, '2026-01-01 00:00:00');
        INSERT INTO task_sessions(id, task_id, started_at) VALUES(99, 99, '2026-01-01 00:00:00');
        CREATE TABLE tool_call_stats(session_id INTEGER, task_id INTEGER, tool_name TEXT,
            call_count INTEGER, total_cost REAL, max_cost REAL, tokens_out INTEGER, tokens_in INTEGER,
            computed_at TEXT, UNIQUE(session_id, tool_name));
        CREATE TABLE tool_call_events(task_id INTEGER, session_id INTEGER, tool_name TEXT,
            cost_dollars REAL, tokens_in INTEGER, tokens_out INTEGER, call_sequence INTEGER, called_at TEXT);
    ''')
    path = tmp_path / 'rollout.jsonl'
    event = {'hook_event_name': 'SessionEnd', 'session_id': 'thr_42',
             'cwd': str(tmp_path), 'transcript_path': str(path)}
    context = {'root': str(tmp_path), 'db_path': str(tmp_path / 'fake.db'),
               'task_id': 42, 'session_id': 43, 'codex_session_id': 'thr_42'}
    def write(model='gpt-6-sol', tokens=1000):
        entries = [
            {'type': 'session_meta', 'payload': {'id': 'thr_42', 'cwd': str(tmp_path)}},
            {'type': 'turn_context', 'payload': {'model': model}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'call1', 'name': 'exec_command'}},
            {'type': 'event_msg', 'timestamp': '2026-01-01T00:00:10Z', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': tokens, 'cached_input_tokens': 100, 'output_tokens': 20}}}},
        ]
        path.write_text(''.join(json.dumps(e) + '\n' for e in entries))
    def row():
        return dict(conn.execute('SELECT * FROM task_sessions WHERE id=43').fetchone())
    yield stats, conn, path, event, context, write, row
    conn.close()


def test_contract_1(telemetry):
    stats, conn, path, event, context, write, row = telemetry
    write()
    assert stats.capture_hook(conn, context, event) == {}
    first = row()
    assert first['tokens_in'] == 1000 and first['tokens_out'] == 20
    assert first['cost_dollars'] > 0 and first['telemetry_status'] == 'captured'
    assert first['ended_at'] is None
    assert stats.capture_hook(conn, context, event) == {}
    assert row() == first
    assert conn.execute('SELECT sum(call_count) FROM tool_call_stats WHERE session_id=43').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM tool_call_events WHERE session_id=43').fetchone()[0] == 1
    assert conn.execute('SELECT tokens_in FROM task_sessions WHERE id=99').fetchone()[0] is None
    # Late events may replace metrics on an exactly identified closed session.
    conn.execute("UPDATE task_sessions SET ended_at='2026-01-01 00:00:20' WHERE id=43")
    assert stats.capture_hook(conn, context, event) == {}
    assert row()['ended_at'] == '2026-01-01 00:00:20'
    assert conn.execute('SELECT status FROM tasks WHERE id=42').fetchone()[0] == 'In Progress'


def test_contract_2(telemetry):
    stats, conn, path, event, context, write, row = telemetry
    write('unknown-model-' + 'x' * 300)
    message = stats.capture_hook(conn, context, event)['systemMessage']
    assert len(message) < 400 and 'pricing.json' in message and 'session-stats 43' in message
    assert row()['cost_dollars'] is None and row()['tokens_in'] == 1000
    assert conn.execute('SELECT count(*) FROM tool_call_stats').fetchone()[0] == 0
    write()
    stats.capture_hook(conn, context, event)
    valid = row()
    assert valid['cost_dollars'] > 0
    write(tokens=500)
    assert 'partial' in stats.capture_hook(conn, context, event)['systemMessage']
    assert row() == valid
    path.write_text('')
    assert 'deferred' in stats.capture_hook(conn, context, event)['systemMessage']
    assert row() == valid
    write()
    path.write_text(path.read_text().replace('"thr_42"', '"other-thread"'))
    assert 'identity' in stats.capture_hook(conn, context, event)['systemMessage']
    assert row() == valid


def test_contract_3(telemetry, monkeypatch):
    stats, conn, path, event, context, write, row = telemetry
    hooks = load('tusk-hooks-codex')
    assert hooks.definitions()['SessionEnd'][0]['hooks'][0]['timeout'] == 3
    def timeout(*args, **kwargs):
        assert 0 < kwargs['timeout'] <= 2
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(hooks.subprocess, 'run', timeout)
    assert 'session-stats 43' in hooks.session_end(event, context)['systemMessage']
    assert row()['tokens_in'] is None and row()['ended_at'] is None
    write()
    monkeypatch.setattr(stats, 'HOOK_TRANSCRIPT_LIMIT', 10)
    assert 'budget' in stats.capture_hook(conn, context, event)['systemMessage']
    assert row()['cost_dollars'] is None
    monkeypatch.setenv('TUSK_CODEX_TELEMETRY', '0')
    monkeypatch.setattr(hooks, 'resolve_context', lambda _: pytest.fail('opt-out must skip reads'))
    import io
    assert hooks.dispatch(io.StringIO(json.dumps(event))) == {}


def test_worker_missing_and_malformed_preserves_data(telemetry):
    stats, conn, path, event, context, write, row = telemetry
    write()
    stats.capture_hook(conn, context, event)
    disk = sqlite3.connect(context['db_path'])
    conn.backup(disk)
    disk.close()
    hooks = load('tusk-hooks-codex')
    for content in ('{broken\n', ''):
        path.write_text(content)
        assert 'systemMessage' in hooks.capture_worker({'event': event, 'context': context})
    path.unlink()
    assert 'retained' in hooks.capture_worker({'event': event, 'context': context})['systemMessage']
    with sqlite3.connect(context['db_path']) as saved:
        assert saved.execute('SELECT tokens_in, cost_dollars, ended_at FROM task_sessions WHERE id=43').fetchone() == (1000, row()['cost_dollars'], None)


def test_partial_tool_snapshot_preserves_calls(telemetry):
    stats, conn, path, event, context, write, row = telemetry
    write()
    stats.capture_hook(conn, context, event)
    before = row()
    path.write_text(''.join(line + '\n' for line in path.read_text().splitlines() if 'function_call' not in line))
    assert 'fewer tool calls' in stats.capture_hook(conn, context, event)['systemMessage']
    assert row() == before
