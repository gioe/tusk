"""Prompt snapshots survive their source and safely identify pre-task intent."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'bin' / 'tusk'


def run(db_path, *args, content=None, ok=True, env_extra=None):
    env = dict(os.environ, TUSK_DB=str(db_path), TUSK_NO_BACKUP='1')
    env.update(env_extra or {})
    result = subprocess.run([str(CLI), 'provenance', *map(str, args)],
                            input=None if content is None else content.encode('utf-8'),
                            cwd=ROOT, env=env, capture_output=True, timeout=30)
    if not ok:
        assert result.returncode != 0, result.stdout
        assert b'Traceback' not in result.stderr
        return result.stderr.decode('utf-8')
    assert result.returncode == 0, result.stderr.decode('utf-8')
    return json.loads(result.stdout)


def capture(db_path, text, *args, **kwargs):
    return run(db_path, 'capture-prompt', '--stdin', '--representation', 'excerpt',
               *args, content=text, **kwargs)


def row_counts(db_path):
    with sqlite3.connect(db_path) as conn:
        return tuple(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('provenance_records', 'provenance_prompts', 'provenance_links'))


def test_capture_before_task(db_path):
    prompt = capture(db_path, 'Never create duplicate charges.', '--provider', 'codex',
                     '--conversation-id', 'thread-1', '--message-id', 'message-2')
    assert prompt['prompt']['identity_status'] == 'complete'
    assert prompt['prompt']['provider'] == 'codex'
    assert prompt['prompt']['conversation_id'] == 'thread-1'
    assert prompt['prompt']['message_id'] == 'message-2'
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        conn.execute("INSERT INTO tasks(id, summary) VALUES (1, 'API behavior')")
        conn.execute("INSERT INTO tasks(id, summary) VALUES (2, 'UI behavior')")
        conn.execute("INSERT INTO acceptance_criteria(id, task_id, criterion) VALUES (1, 1, 'One charge')")
    for kind, native_id in [('task', 1), ('task', 2), ('criterion', 1)]:
        record = run(db_path, 'register', kind, native_id)
        run(db_path, 'link', record['ref'], 'derived_from', prompt['ref'])
    incoming = run(db_path, 'links', prompt['ref'], '--direction', 'incoming')
    assert len(incoming['links']) == 3
    assert run(db_path, 'get', prompt['ref']) == prompt


def test_safe_capture_and_retry(db_path, tmp_path):
    text = "  'quoted' \"double\" `touch should-not-exist` $(echo unsafe) ${HOME} $USER \\ path\r\nUnicode: café 🐘\r\n"
    path = tmp_path / 'selected.txt'
    path.write_bytes(text.encode('utf-8'))
    identity = ('--provider', 'claude', '--conversation-id', 'c1', '--message-id', 'm1')
    original = run(db_path, 'capture-prompt', '--file', path, '--representation', 'excerpt', *identity)
    assert original['prompt']['content'] == text
    assert capture(db_path, text, *identity) == original
    # Message IDs are namespaced by both provider and conversation.
    other = capture(db_path, text, '--provider', 'codex', '--conversation-id', 'c1', '--message-id', 'm1')
    third = capture(db_path, text, '--provider', 'claude', '--conversation-id', 'c2', '--message-id', 'm1')
    assert len({original['ref'], other['ref'], third['ref']}) == 3
    before = row_counts(db_path)
    assert 'immutable' in capture(db_path, 'Changed text', *identity, ok=False)
    capture(db_path, text, *identity, '--key', 'different-key', ok=False)
    assert row_counts(db_path) == before
    unknown = capture(db_path, text, env_extra={'CODEX_THREAD_ID': 'must-not-infer'})
    assert unknown['prompt']['identity_status'] == 'unknown'
    assert all(unknown['prompt'][key] is None for key in ('provider', 'conversation_id', 'message_id'))
    assert capture(db_path, text, '--key', unknown['external_key']) == unknown
    assert capture(db_path, text)['ref'] != unknown['ref']  # Equal text is not identity.
    partial = capture(db_path, text, '--provider', 'codex', '--key', 'caller-retry-key')
    assert partial['prompt']['identity_status'] == 'partial'
    assert partial['prompt']['message_id'] is None
    assert capture(db_path, text, '--provider', 'codex', '--key', 'caller-retry-key') == partial
    assert not (ROOT / 'should-not-exist').exists()


def test_durable_source_snapshot(db_path, tmp_path):
    source = tmp_path / 'transcript.txt'
    source.write_text('Original full conversation which must never be ingested automatically.', encoding='utf-8')
    text = 'Selected excerpt only.\n'
    prompt = capture(db_path, text, '--locator', str(source))
    source.unlink()
    assert run(db_path, 'get', prompt['ref'])['prompt']['content'] == text
    assert prompt['prompt']['representation'] == 'excerpt'
    assert prompt['prompt']['truncated'] is False
    summary = run(db_path, 'capture-prompt', '--stdin', '--representation', 'summary',
                  content='Agent-authored summary.')
    assert summary['prompt']['representation'] == 'summary'
    clipped = capture(db_path, 'abcdefghij', '--max-chars', 5)
    assert clipped['prompt']['content'] == 'abcde' and clipped['prompt']['truncated'] is True
    exact = capture(db_path, 'abcde', '--max-chars', 5)
    assert exact['prompt']['truncated'] is False
    declared = capture(db_path, 'abcde', '--truncated')
    assert declared['prompt']['truncated'] is True
    default = capture(db_path, 'a' * 9000)
    assert len(default['prompt']['content']) == 8192
    assert default['prompt']['truncated'] is True
    run(db_path, 'unavailable', prompt['ref'])
    retained = run(db_path, 'get', prompt['ref'])
    assert retained['availability'] == 'unavailable'
    assert retained['prompt']['content'] == text


def test_capture_errors_are_atomic(db_path, tmp_path):
    before = row_counts(db_path)
    for text, flags in [('', ()), (' \n\t', ()), ('valid', ('--max-chars', '0')),
                        ('valid', ('--max-chars', '65537')), ('valid', ('--provider', ' ')),
                        ('valid', ('--key', '')), ('valid', ('--locator', ' ')), ('a\x00b', ())]:
        capture(db_path, text, *flags, ok=False)
    run(db_path, 'capture-prompt', '--file', tmp_path / 'missing', '--representation', 'excerpt', ok=False)
    bad = tmp_path / 'bad.txt'
    bad.write_bytes(b'\xff')
    run(db_path, 'capture-prompt', '--file', bad, '--representation', 'excerpt', ok=False)
    run(db_path, 'capture-prompt', '--stdin', content='No representation', ok=False)
    assert row_counts(db_path) == before
    identity = run(db_path, 'register', 'prompt', '--key', 'legacy')
    assert identity['prompt'] is None
    captured = capture(db_path, 'Attach selected text', '--key', 'legacy')
    assert captured['ref'] == identity['ref']
    unavailable = run(db_path, 'register', 'prompt', '--key', 'unavailable')
    run(db_path, 'unavailable', unavailable['ref'])
    before = row_counts(db_path)
    capture(db_path, 'Must not attach', '--key', 'unavailable', ok=False)
    assert row_counts(db_path) == before
    with sqlite3.connect(db_path) as conn:
        for statement in ["UPDATE provenance_prompts SET content = 'changed'",
                          'DELETE FROM provenance_prompts',
                          'INSERT OR REPLACE INTO provenance_prompts SELECT * FROM provenance_prompts']:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
            conn.rollback()


def test_concurrent_capture(db_path):
    def invoke(_):
        return capture(db_path, 'One durable message.', '--provider', 'codex',
                       '--conversation-id', 'shared-thread', '--message-id', 'shared-message')
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(invoke, range(4)))
    assert all(record == records[0] for record in records)
    assert row_counts(db_path) == (1, 1, 0)


def test_prompt_migration(db_path, config_path, tmp_path):
    legacy = run(db_path, 'register', 'prompt', '--key', 'legacy')
    with sqlite3.connect(db_path) as fresh:
        expected = fresh.execute("SELECT type, name, sql FROM sqlite_master WHERE name = 'provenance_prompts' OR name LIKE 'provenance_prompt_%' OR name LIKE 'idx_provenance_prompt_%' ORDER BY name").fetchall()
        migrated = tmp_path / 'migrated.db'
        with sqlite3.connect(migrated) as conn:
            fresh.backup(conn)
            for name, in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'provenance_prompt_%'").fetchall():
                conn.execute(f'DROP TRIGGER {name}')
            conn.execute('DROP TABLE provenance_prompts')
            conn.execute('PRAGMA user_version = 89')
    assert run(migrated, 'get', legacy['ref'])['prompt'] is None
    assert 'run tusk migrate' in capture(migrated, 'Not migrated', ok=False)
    spec = importlib.util.spec_from_file_location('prompt_migration', ROOT / 'bin/tusk-migrate.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate_90(str(migrated), config_path, str(ROOT / 'bin'))
    migration.migrate_90(str(migrated), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(migrated) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 90
        assert expected == conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name = 'provenance_prompts' OR name LIKE 'provenance_prompt_%' OR name LIKE 'idx_provenance_prompt_%' ORDER BY name").fetchall()
    assert capture(migrated, 'Snapshot after migration', '--key', 'legacy')['ref'] == legacy['ref']
