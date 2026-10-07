"""Durable observation dispositions through the public CLI and retro contract."""
import json
from pathlib import Path
import re
import sqlite3

import pytest

from tests.integration.test_observation_storage import run

ROOT = Path(__file__).resolve().parents[2]
LEGACY_FIELDS = {'id', 'skill_run_id', 'task_id', 'category', 'note', 'file_hint', 'skill_hint', 'created_at'}


@pytest.fixture
def captures(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
          INSERT INTO tasks(id,summary) VALUES(1,'Observation host'),(2,'Approved follow-up');
          INSERT INTO skill_runs(id,skill_name,task_id) VALUES(1,'tusk',1);
        """)
    def capture(note):
        return run(db_path, 'jot', 'write', 'workflow', note, '--file', 'bin/tusk', '--skill', 'retro', '--task-id', 1)
    return capture


def register(db, kind, identity):
    return run(db, 'provenance', 'register', kind, identity)['ref']


def atom_id(db, jot_id):
    with sqlite3.connect(db) as conn:
        return conn.execute('SELECT context_id FROM jot_aliases WHERE id=?', (jot_id,)).fetchone()[0]


def listed(db, status='all'):
    return run(db, 'jots', '--task-id', 1, '--triage-status', status)


def captured_rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute('SELECT * FROM jots ORDER BY id').fetchall()


def domain_counts(db):
    with sqlite3.connect(db) as conn:
        return tuple(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('tasks', 'acceptance_criteria', 'task_context_items', 'provenance_records', 'provenance_links'))


def test_dispositions_preserve_capture(db_path, captures):
    dismissed = captures('An obsolete workflow concern')
    pending = captures('Still needs investigation')
    before = captured_rows(db_path)
    original_ref = register(db_path, 'jot', dismissed['id'])
    assert [r['id'] for r in listed(db_path, 'pending')] == [pending['id'], dismissed['id']]
    assert all(r['triage_status'] == 'pending' for r in listed(db_path))
    error = run(db_path, 'jot', 'dismiss', dismissed['id'], '--reason', '  ', ok=False)
    assert 'reason' in error.lower()
    assert len(listed(db_path, 'pending')) == 2
    result = run(db_path, 'jot', 'dismiss', dismissed['id'], '--reason', 'Workflow was intentionally removed')
    assert 'Workflow was intentionally removed' in json.dumps(result)
    assert captured_rows(db_path) == before
    terminal = listed(db_path, 'dismissed')
    assert [r['id'] for r in terminal] == [dismissed['id']]
    assert terminal[0]['triage_status'] == 'dismissed'
    assert 'Workflow was intentionally removed' in json.dumps(terminal)
    assert [r['id'] for r in listed(db_path, 'pending')] == [pending['id']]
    assert listed(db_path, 'promoted') == []
    assert register(db_path, 'jot', dismissed['id']) == original_ref
    source_id = atom_id(db_path, dismissed['id'])
    with sqlite3.connect(db_path) as conn:
        # sqlite clients default to FK-off: append-only outcome/capture guards
        # must still prevent silent history loss or rewriting triaged evidence.
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 0
        for sql, params in (
            ('DELETE FROM observation_dispositions WHERE context_id=?', (source_id,)),
            ("UPDATE observation_dispositions SET reason='Rewritten' WHERE context_id=?", (source_id,)),
            ('DELETE FROM task_context_items WHERE id=?', (source_id,)),
            ('DELETE FROM jot_aliases WHERE id=?', (dismissed['id'],)),
            ('DELETE FROM jots WHERE id=?', (dismissed['id'],)),
            ("UPDATE jots SET note='Rewritten capture' WHERE id=?", (dismissed['id'],)),
            ("UPDATE task_context_items SET triage_status='pending' WHERE id=?", (source_id,)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)
            conn.rollback()
    assert captured_rows(db_path) == before
    assert all(set(row) == LEGACY_FIELDS for row in run(db_path, 'jots', '--task-id', 1))
    assert run(db_path, 'jots', '--task-id', 2, '--triage-status', 'all') == []
    assert run(db_path, 'jots', '--skill-run-id', 1, '--triage-status', 'dismissed') == terminal


def test_promotion_destinations_and_provenance(db_path, captures):
    decision = run(db_path, 'context', 'add', 1, '--type', 'decision', '--content', 'Approved decision')
    risk = run(db_path, 'context', 'add', 1, '--type', 'risk', '--content', 'Approved risk')
    with sqlite3.connect(db_path) as conn:
        criterion = conn.execute("INSERT INTO acceptance_criteria(task_id,criterion) VALUES(1,'Approved criterion')").lastrowid
    targets = [('context', decision['id']), ('context', risk['id']), ('criterion', criterion), ('task', 2)]
    notes = []
    for kind, target_id in targets:
        source = captures(f'UNTRIAGED SOURCE {kind} {target_id}')
        notes.append(source['note'])
        dest_ref = register(db_path, kind, target_id)
        jot_ref = register(db_path, 'jot', source['id'])
        context_ref = register(db_path, 'context', atom_id(db_path, source['id']))
        capture = captured_rows(db_path)
        run(db_path, 'jot', 'promote', source['id'], '--to', dest_ref)
        assert captured_rows(db_path) == capture
        terminal = next(r for r in listed(db_path, 'promoted') if r['id'] == source['id'])
        assert dest_ref in json.dumps(terminal)
        links = run(db_path, 'provenance', 'links', dest_ref)['links']
        assert {jot_ref, context_ref} <= {row['target_ref'] for row in links if row['relationship'] == 'derived_from' and row['source_ref'] == dest_ref}
        assert run(db_path, 'provenance', 'get', dest_ref)['availability'] == 'available'
        with sqlite3.connect(db_path) as conn:
            assert conn.execute('SELECT triage_status FROM task_context_items WHERE id=?', (atom_id(db_path, source['id']),)).fetchone()[0] == 'promoted'
    assert listed(db_path, 'pending') == []
    brief = json.dumps(run(db_path, 'task-brief', 1))
    assert 'Approved decision' in brief and 'Approved risk' in brief
    assert all(note not in brief for note in notes)


def test_retry_and_failure_safety(db_path, captures):
    source = captures('Retry this promotion safely')
    target = register(db_path, 'task', 2)
    capture = captured_rows(db_path)
    invalid = [target.rsplit(':', 1)[0] + ':' + '0' * 32,
               target.replace(target.split(':')[1], '0' * len(target.split(':')[1]), 1)]
    memory = run(db_path, 'context', 'add', 1, '--type', 'memory', '--content', 'Not a decision or risk')
    invalid.append(register(db_path, 'context', memory['id']))
    removed = run(db_path, 'context', 'add', 1, '--type', 'decision', '--content', 'Deleted outcome')
    invalid.append(register(db_path, 'context', removed['id']))
    with sqlite3.connect(db_path) as conn:
        conn.execute('DELETE FROM task_context_items WHERE id=?', (removed['id'],))
    for destination in invalid:
        before = domain_counts(db_path)
        run(db_path, 'jot', 'promote', source['id'], '--to', destination, ok=False)
        assert domain_counts(db_path) == before
        assert [r['id'] for r in listed(db_path, 'pending')] == [source['id']]
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TRIGGER fail_disposition_test BEFORE UPDATE OF triage_status ON task_context_items
                        WHEN NEW.triage_status='promoted' BEGIN SELECT RAISE(ABORT,'injected disposition failure'); END""")
    before = domain_counts(db_path)
    run(db_path, 'jot', 'promote', source['id'], '--to', target, ok=False)
    assert domain_counts(db_path) == before
    assert captured_rows(db_path) == capture
    assert [r['id'] for r in listed(db_path, 'pending')] == [source['id']]
    with sqlite3.connect(db_path) as conn:
        conn.execute('DROP TRIGGER fail_disposition_test')
    first = run(db_path, 'jot', 'promote', source['id'], '--to', target)
    before = domain_counts(db_path)
    repeat = run(db_path, 'jot', 'promote', source['id'], '--to', target)
    assert repeat == first
    assert domain_counts(db_path) == before
    run(db_path, 'jot', 'dismiss', source['id'], '--reason', 'Conflicts with promotion', ok=False)
    run(db_path, 'jot', 'promote', source['id'], '--to', register(db_path, 'task', 1), ok=False)
    dismissed = captures('Dismiss once')
    first = run(db_path, 'jot', 'dismiss', dismissed['id'], '--reason', 'Already addressed elsewhere')
    assert run(db_path, 'jot', 'dismiss', dismissed['id'], '--reason', 'Already addressed elsewhere') == first
    run(db_path, 'jot', 'dismiss', dismissed['id'], '--reason', 'Different conclusion', ok=False)
    run(db_path, 'jot', 'promote', dismissed['id'], '--to', target, ok=False)
    assert len(listed(db_path, 'promoted')) == 1 and len(listed(db_path, 'dismissed')) == 1


def test_retro_workflow_parity():
    for path in ('skills/retro/SKILL.md', 'skills/retro/FULL-RETRO.md', 'codex-prompts/retro.md'):
        text = (ROOT / path).read_text()
        assert re.search(r'tusk jots[^\n]*--task-id[^\n]*--triage-status pending', text), path
        assert re.search(r'tusk jot promote[^\n]*--to', text), path
        assert re.search(r'tusk jot dismiss[^\n]*--reason', text), path
        if path.endswith('FULL-RETRO.md'):
            assert '--triage-status all' in text or "SKILL.md's shared pending" in text, path
        else:
            assert '--triage-status all' in text, path
        assert 'approval' in text.lower(), path
        assert re.search(r'recover|reuse|existing destination', text, re.I), path


def test_triage_migration_preserves_observations(db_path, captures, config_path):
    from tests.integration.test_observation_storage import migration
    source = captures('Awaiting triage before schema upgrade')
    source_ref = register(db_path, 'jot', source['id'])
    objects = "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'observation_disposition%' ORDER BY name"
    before = captured_rows(db_path)
    with sqlite3.connect(db_path) as conn:
        fresh_ddl = conn.execute(objects).fetchall()
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'observation_disposition%'").fetchall():
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute('DROP TABLE observation_dispositions')
        conn.execute('PRAGMA user_version=93')
    module = migration()
    module.migrate_94(str(db_path), config_path, str(ROOT / 'bin'))
    assert captured_rows(db_path) == before
    assert run(db_path, 'provenance', 'get', source_ref)['availability'] == 'available'
    assert [row['id'] for row in listed(db_path, 'pending')] == [source['id']]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(objects).fetchall() == fresh_ddl
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 94
        snapshot = list(conn.iterdump())
    module.migrate_94(str(db_path), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(db_path) as conn:
        assert list(conn.iterdump()) == snapshot
