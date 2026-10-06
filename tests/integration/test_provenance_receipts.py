"""Receipts describe committed mutations, never inferred intent or proof."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'bin/tusk'


def run(db_path, *args, content=None, ok=True, cwd=ROOT, env_extra=None):
    env = dict(os.environ, TUSK_DB=str(db_path), TUSK_NO_BACKUP='1', TUSK_PROJECT=str(ROOT))
    env.update(env_extra or {})
    result = subprocess.run([str(CLI), *map(str, args)], input=content, cwd=cwd,
                            env=env, capture_output=True, text=True, timeout=60)
    assert (result.returncode == 0) == ok, (result.stdout, result.stderr)
    assert 'Traceback' not in result.stderr, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def receipts(db_path, result):
    assert result['receipt_refs']
    return [run(db_path, 'provenance', 'get', ref)['receipt'] for ref in result['receipt_refs']]


def count(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute('SELECT count(*) FROM provenance_actions').fetchone()[0]


def insert(db_path, summary='A receipt fixture'):
    return run(db_path, 'task-insert', summary, 'A shippable receipt test', '--skip-dupe',
               '--criteria', 'Behavior is preserved')


def test_atomic_receipts(db_path):
    result = insert(db_path)
    task = result['task_id']
    criterion = result['criteria_ids'][0]
    effects = receipts(db_path, result)[0]['effects']
    assert {(e['kind'], e['native_id'], e['operation']) for e in effects} >= {
        ('task', task, 'insert'), ('criterion', criterion, 'insert')}
    assert receipts(db_path, run(db_path, 'task-update', task, '--summary', 'Revised fixture'))
    started = receipts(db_path, run(db_path, 'task-start', task, '--force', '--skill', 'tusk'))
    assert len({r['invocation_id'] for r in started}) == 1
    assert {e['kind'] for r in started for e in r['effects']} >= {'task', 'session', 'skill_run'}
    ctx = run(db_path, 'context', 'add', task, '--type', 'decision', '--content', 'Retain the source')
    assert receipts(db_path, ctx)[0]['command'] == 'context add'
    assert receipts(db_path, run(db_path, 'context', 'resolve', ctx['id']))
    assert receipts(db_path, run(db_path, 'context', 'supersede', ctx['id']))
    assert receipts(db_path, run(db_path, 'progress', task, '--note', 'Checkpoint'))
    added = run(db_path, 'criteria', 'add', task, 'An extra promise')
    assert receipts(db_path, added)
    assert receipts(db_path, run(db_path, 'criteria', 'update', added['id'], '--text', 'Edited promise'))
    assert receipts(db_path, run(db_path, 'criteria', 'skip', added['id'], '--reason', 'not applicable'))
    assert receipts(db_path, run(db_path, 'criteria', 'finish-deferred', '--reason', 'not applicable', task))
    assert receipts(db_path, run(db_path, 'criteria', 'reset', added['id']))
    deleted = receipts(db_path, run(db_path, 'criteria', 'delete', added['id']))[0]['effects']
    ref = next(e['ref'] for e in deleted if e['kind'] == 'criterion' and e['operation'] == 'delete')
    assert run(db_path, 'provenance', 'get', ref)['availability'] == 'unavailable'
    assert receipts(db_path, run(db_path, 'criteria', 'done', criterion, '--skip-verify'))
    assert receipts(db_path, run(db_path, 'task-done', task, '--reason', 'completed', '--force'))

    payload = {'tasks': [
        {'key': 'a', 'summary': 'Imported alpha', 'description': 'Import atomic fixture', 'criteria': ['Imported criterion']},
        {'key': 'b', 'summary': 'Imported beta', 'description': 'Import dependency fixture', 'criteria': ['Dependent behavior'], 'depends_on': ['a']}]}
    imported = run(db_path, 'task-import', '--stdin', content=json.dumps(payload))
    batch = receipts(db_path, imported)
    assert len(batch) == 1
    ids = {entry['task_id'] for entry in imported['created'].values()}
    assert ids <= {e['native_id'] for e in batch[0]['effects'] if e['kind'] == 'task'}
    assert 'task_id' not in batch[0]['context']
    assert 'task_id' in batch[0]['context']['ambiguous']

    before = count(db_path)
    with sqlite3.connect(db_path) as conn:
        tasks_before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON provenance_action_effects "
                     "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END")
    run(db_path, 'task-insert', 'Must roll back', 'No partial mutation', '--skip-dupe', ok=False)
    assert count(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == tasks_before
        conn.execute('DROP TRIGGER reject_receipt')
    # A late dependency failure rolls back both materialized tasks and receipt.
    broken = {'tasks': [{'key': 'broken', 'summary': 'Late rollback', 'description': 'Late failure fixture', 'criteria': ['Rollback'], 'depends_on': [task], 'duplicate_policy': 'allow'}]}
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TRIGGER reject_dependency BEFORE INSERT ON task_dependencies "
                     "BEGIN SELECT RAISE(ABORT, 'injected dependency failure'); END")
    run(db_path, 'task-import', '--stdin', content=json.dumps(broken), ok=False)
    assert count(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == tasks_before
    # Best effort retains a receipt for creation even when the link phase fails.
    partial = run(db_path, 'task-import', '--stdin', '--best-effort', content=json.dumps(broken), ok=False)
    assert partial['created'] and partial['failed']
    assert receipts(db_path, partial)
    listed = run(db_path, 'provenance', 'receipts', '--task-id', task, '--limit', 1)
    assert len(listed['receipts']) == 1 and listed['truncated'] is True


def test_execution_attribution(db_path, tmp_path):
    a = insert(db_path, 'First task')['task_id']
    b = insert(db_path, 'Second task')['task_id']
    paths = [tmp_path / 'a', tmp_path / 'b']
    for p in paths:
        p.mkdir()
    with sqlite3.connect(db_path) as conn:
        for i, (task, path) in enumerate(zip((a, b), paths), 1):
            conn.execute('INSERT INTO task_workspaces(id,task_id,branch,workspace_path) VALUES (?,?,?,?)',
                         (i, task, f'feature/{i}', str(path)))
            conn.execute("INSERT INTO task_sessions(id,task_id,started_at) VALUES (?,?,datetime('now'))", (i, task))
            conn.execute("INSERT INTO skill_runs(id,skill_name,task_id) VALUES (?,'tusk',?)", (i, task))
    def mutate(pair):
        task, path = pair
        return run(db_path, 'context', 'add', task, '--type', 'memory', '--content', 'Concurrent change', cwd=path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(mutate, zip((a, b), paths)))
    for i, output in enumerate(outputs, 1):
        ctx = receipts(db_path, output)[0]['context']
        assert (ctx['task_id'], ctx['session_id'], ctx['skill_run_id'], ctx['workspace_id']) == ((a, b)[i-1], i, i, i)
        assert ctx['attribution']['task_id'] == 'caller_workspace'
    prompt = run(db_path, 'provenance', 'capture-prompt', '--stdin', '--representation', 'excerpt', content='A request')
    explicit = {'TUSK_ACTION_TASK_ID': str(b), 'TUSK_ACTION_SESSION_ID': '2',
                'TUSK_ACTION_SKILL_RUN_ID': '2', 'TUSK_ACTION_WORKSPACE_ID': '2',
                'TUSK_ACTION_SOURCE_REF': prompt['ref']}
    output = run(db_path, 'context', 'add', a, '--type', 'memory', '--content', 'Cross task work',
                 cwd=paths[0], env_extra=explicit)
    ctx = receipts(db_path, output)[0]['context']
    assert ctx['task_id'] == b and ctx['workspace_id'] == 2 and ctx['source_ref'] == prompt['ref']
    assert ctx['attribution']['task_id'] == 'explicit'
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO skill_runs(skill_name,task_id) VALUES ('review-commits',?)", (a,))
    ctx = receipts(db_path, mutate((a, paths[0])))[0]['context']
    assert 'skill_run_id' not in ctx and 'skill_run_id' in ctx['ambiguous']
    before = count(db_path)
    run(db_path, 'context', 'add', a, '--type', 'memory', '--content', 'Invalid attribution',
        env_extra={'TUSK_ACTION_TASK_ID': str(a), 'TUSK_ACTION_SESSION_ID': '2'}, ok=False)
    assert count(db_path) == before


def test_retry_and_output_contract(db_path):
    result = insert(db_path)
    task = result['task_id']
    assert set(result) == {'task_id', 'summary', 'criteria_ids', 'receipt_refs'}
    before = count(db_path)
    run(db_path, 'context', 'add', 999999, '--type', 'memory', '--content', 'Invalid', ok=False)
    assert count(db_path) == before
    sys.path.insert(0, str(ROOT / 'bin'))
    import tusk_loader
    actions = tusk_loader.load('tusk-action-lib')
    db = tusk_loader.load('tusk-db-lib')
    action = actions.CLIAction('retry-fixture')
    attempts = []
    def attempt():
        conn = action.get_connection(str(db_path))
        try:
            conn.execute('UPDATE tasks SET summary = ? WHERE id = ?', ('Retried mutation', task))
            attempts.append(1)
            if len(attempts) == 1:
                # Even failure after receipt preparation must roll back both.
                action.flush(conn)
                raise sqlite3.OperationalError('database is locked')
            conn.commit()
        finally:
            conn.close()
    db.retry_on_locked(attempt, retries=1, base_ms=0)
    assert len(attempts) == 2 and count(db_path) == before + 1
    assert len(action.refs) == 1
    conn = action.get_connection(str(db_path))
    conn.execute('BEGIN IMMEDIATE')
    conn.execute('SAVEPOINT speculative')
    conn.execute('UPDATE tasks SET summary = ? WHERE id = ?', ('Discarded speculation', task))
    conn.execute('ROLLBACK TO speculative')
    conn.execute('RELEASE speculative')
    conn.commit()
    conn.close()
    assert count(db_path) == before + 1
    # No-op commit and rolled-back effects do not emit another receipt.
    conn = action.get_connection(str(db_path))
    conn.commit()
    conn.execute('UPDATE tasks SET summary = ? WHERE id = ?', ('Must roll back', task))
    conn.rollback()
    conn.commit()
    conn.close()
    assert count(db_path) == before + 1
    assert run(db_path, 'task-get', task)['task']['summary'] == 'Retried mutation'

    # Real SQLite COMMIT contention: a rollback-journal reader permits writes
    # but blocks COMMIT. Retrying that COMMIT must retain exactly one receipt.
    journal_path = db_path.with_name('journal.db')
    source = sqlite3.connect(db_path)
    raw = sqlite3.connect(journal_path)
    source.backup(raw)
    source.close()
    raw.execute('PRAGMA journal_mode = DELETE')
    raw.close()
    reader = sqlite3.connect(journal_path)
    reader.execute('BEGIN')
    reader.execute('SELECT * FROM tasks').fetchall()
    conn = action.get_connection(str(journal_path))
    conn.execute('PRAGMA busy_timeout = 0')
    conn.execute('UPDATE tasks SET summary = ? WHERE id = ?', ('Commit retry', task))
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            conn.commit()
        reader.rollback()
        conn.commit()
    finally:
        reader.close()
        conn.close()
    assert count(journal_path) == before + 2


def test_receipt_is_not_proof(db_path):
    task = insert(db_path)['task_id']
    result = run(db_path, 'criteria', 'add', task, 'A failing check', '--type', 'test', '--spec', 'false')
    criterion = result['id']
    receipt = receipts(db_path, result)[0]
    assert receipt['outcome'] == 'mutation_committed' and receipt['is_verification'] is False
    assert run(db_path, 'provenance', 'links', result['receipt_refs'][0])['links'] == []
    before = count(db_path)
    run(db_path, 'criteria', 'done', criterion, ok=False)
    assert count(db_path) == before + 1  # records the failed verification result's mutation
    with sqlite3.connect(db_path) as conn:
        row = conn.execute('SELECT is_completed,verification_result FROM acceptance_criteria WHERE id = ?', (criterion,)).fetchone()
        assert row[0] == 0 and json.loads(row[1])['passed'] is False
        assert conn.execute("SELECT count(*) FROM provenance_links WHERE relationship = 'verifies'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM provenance_records WHERE kind = 'evidence'").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute("UPDATE provenance_actions SET outcome = 'mutation_committed'")


def test_receipt_migration(db_path, config_path, tmp_path):
    migrated = tmp_path / 'migration.db'
    with sqlite3.connect(db_path) as fresh, sqlite3.connect(migrated) as conn:
        fresh.backup(conn)
        objects = "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'provenance_action%' OR name LIKE 'idx_provenance_action%' ORDER BY name"
        expected = conn.execute(objects).fetchall()
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'provenance_action%'").fetchall():
            conn.execute(f'DROP TRIGGER {name}')
        conn.execute('DROP TABLE provenance_action_effects')
        conn.execute('DROP TABLE provenance_actions')
        conn.execute('PRAGMA user_version = 90')
    spec = importlib.util.spec_from_file_location('receipt_migration', ROOT / 'bin/tusk-migrate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for _ in range(2):
        module.migrate_91(str(migrated), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(migrated) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 91
        assert conn.execute(objects).fetchall() == expected
        assert conn.execute('SELECT count(*) FROM provenance_actions').fetchone()[0] == 0
