"""Public provenance contract against isolated fresh and migrated databases."""

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


def run(db_path, *args, ok=True):
    env = dict(os.environ, TUSK_DB=str(db_path), TUSK_NO_BACKUP='1')
    result = subprocess.run([str(CLI), 'provenance', *map(str, args)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    if not ok:
        assert result.returncode != 0, result.stdout
        assert 'Traceback' not in result.stderr
        return result.stderr
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def seed(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            INSERT INTO tasks(id, summary) VALUES (1, 'Provenance fixture');
            INSERT INTO objectives(id, summary) VALUES (1, 'Intent');
            INSERT INTO acceptance_criteria(id, task_id, criterion) VALUES (1, 1, 'Promise');
            INSERT INTO task_context_items(id, task_id, item_type, content) VALUES (1, 1, 'decision', 'Old decision');
            INSERT INTO task_context_items(id, task_id, item_type, content) VALUES (2, 1, 'decision', 'New decision');
            INSERT INTO code_reviews(id, task_id) VALUES (1, 1);
            INSERT INTO review_comments(id, review_id, comment) VALUES (1, 1, 'Finding');
            INSERT INTO task_sessions(id, task_id, started_at) VALUES (1, 1, datetime('now'));
            INSERT INTO skill_runs(id, skill_name, task_id) VALUES (1, 'tusk', 1);
            INSERT INTO task_progress(id, task_id, note) VALUES (1, 1, 'Checkpoint');
            INSERT INTO jots(id, skill_run_id, category, note) VALUES (1, 1, 'memory', 'Jot');
            INSERT INTO retro_findings(id, skill_run_id, category, summary) VALUES (1, 1, 'memory', 'Learning');
        """)


def counts(db_path):
    with sqlite3.connect(db_path) as conn:
        return tuple(conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                     for t in ('provenance_records', 'provenance_links'))


def test_reference_identity(db_path, tmp_path):
    seed(db_path)
    kinds = ('objective', 'task', 'criterion', 'context', 'review', 'finding',
             'session', 'skill_run', 'progress', 'jot', 'retro')
    records = [run(db_path, 'register', kind, 1) for kind in kinds]
    assert len({r['ref'] for r in records}) == len(kinds)
    assert len({r['ref'].split(':')[1] for r in records}) == 1
    for kind, record in zip(kinds, records):
        assert run(db_path, 'register', kind, 1) == record
        assert run(db_path, 'get', record['ref']) == record
        assert record['native_id'] == 1
        assert 'content' not in record and 'summary' not in record
    for kind in ('prompt', 'action', 'evidence', 'artifact'):
        ref = run(db_path, 'register', kind, '--key', 'provider:message:1', '--locator', 'https://example.test/1')
        assert run(db_path, 'register', kind, '--key', 'provider:message:1') == ref
    before = counts(db_path)
    for args in [('task', 999), ('task', 0), ('task', 1, '--key', 'no'),
                 ('prompt',), ('prompt', '--key', '  '), ('prompt', 1, '--key', 'no')]:
        run(db_path, 'register', *args, ok=False)
    assert before == counts(db_path)

    other = tmp_path / 'other.db'
    result = subprocess.run([str(CLI), 'init', '--skip-gitignore'],
                            env=dict(os.environ, TUSK_DB=str(other)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'different project' in run(other, 'get', records[0]['ref'], ok=False)
    run(db_path, 'get', records[0]['id'], ok=False)
    with sqlite3.connect(db_path) as conn:
        # Editing domain content does not copy it into a new provenance identity.
        conn.execute("UPDATE tasks SET summary = 'Changed intent' WHERE id = 1")
    assert run(db_path, 'register', 'task', 1) == records[1]


def test_relationship_validation(db_path):
    seed(db_path)
    refs = {kind: run(db_path, 'register', kind, 1)['ref']
            for kind in ('task', 'criterion', 'context', 'finding', 'session')}
    refs.update({kind: run(db_path, 'register', kind, '--key', 'fixture')['ref']
                 for kind in ('prompt', 'action', 'evidence', 'artifact')})
    refs['replacement'] = run(db_path, 'register', 'context', 2)['ref']
    cases = [('task', 'derived_from', 'prompt'), ('context', 'responds_to', 'finding'),
             ('context', 'derived_from', 'action'), ('evidence', 'derived_from', 'artifact'),
             ('context', 'supports', 'criterion'), ('artifact', 'implements', 'criterion'),
             ('evidence', 'verifies', 'artifact'), ('replacement', 'supersedes', 'context')]
    for source, relation, target in cases:
        first = run(db_path, 'link', refs[source], relation, refs[target])
        assert first == run(db_path, 'link', refs[source], relation, refs[target])
    before = counts(db_path)
    for source, relation, target in [('prompt', 'verifies', 'criterion'), ('task', 'supersedes', 'context'),
                                     ('task', 'supports', 'task'), ('session', 'implements', 'criterion')]:
        run(db_path, 'link', refs[source], relation, refs[target], ok=False)
    missing = refs['task'].rsplit(':', 1)[0] + ':' + '0' * 32
    run(db_path, 'link', missing, 'derived_from', refs['prompt'], ok=False)
    assert before == counts(db_path)
    incoming = run(db_path, 'links', refs['criterion'], '--direction', 'incoming')
    assert {l['relationship'] for l in incoming['links']} == {'supports', 'implements'}
    limited = run(db_path, 'links', refs['criterion'], '--limit', 1)
    assert len(limited['links']) == 1 and limited['truncated'] is True
    run(db_path, 'links', refs['criterion'], '--limit', 0, ok=False)


def test_migration_and_retention(db_path, config_path, tmp_path):
    seed(db_path)
    migrated = tmp_path / 'migration.db'
    with sqlite3.connect(db_path) as fresh, sqlite3.connect(migrated) as conn:
        fresh.backup(conn)
        # v90 adds prompt snapshots. Keep this v89 assertion scoped to the
        # objects that existed at v89 rather than drifting with the live schema.
        v89_objects = "SELECT type, name, sql FROM sqlite_master WHERE (name LIKE 'provenance_%' OR name LIKE 'idx_provenance_%') AND name NOT LIKE 'provenance_artifact%' AND name NOT LIKE 'provenance_attempt%' AND name NOT LIKE 'provenance_result%' AND name NOT LIKE 'provenance_review_target%' AND name NOT LIKE 'idx_provenance_attempt%' AND name NOT LIKE 'provenance_action%' AND name NOT LIKE 'idx_provenance_action%' AND name <> 'provenance_prompts' AND name NOT LIKE 'provenance_prompt_%' AND name NOT LIKE 'idx_provenance_prompt_%' ORDER BY name"
        expected = fresh.execute(v89_objects).fetchall()
        # Reconstruct the v88 fixture by removing only this migration's DDL.
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'provenance_%'").fetchall():
            conn.execute(f'DROP TRIGGER {name}')
        for name in ('provenance_results', 'provenance_attempts', 'provenance_review_targets', 'provenance_artifacts', 'provenance_action_effects', 'provenance_actions', 'provenance_prompts', 'provenance_links', 'provenance_records', 'provenance_project'):
            conn.execute(f'DROP TABLE {name}')
        conn.execute('PRAGMA user_version = 88')
    spec = importlib.util.spec_from_file_location('provenance_migration_test', ROOT / 'bin/tusk-migrate.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate_89(str(migrated), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(migrated) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 89
        assert conn.execute('SELECT summary FROM tasks WHERE id = 1').fetchone()[0] == 'Provenance fixture'
        assert expected == conn.execute(v89_objects).fetchall()
        identity = conn.execute('SELECT project_id FROM provenance_project').fetchone()[0]
    migration.migrate_89(str(migrated), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(migrated) as conn:
        assert identity == conn.execute('SELECT project_id FROM provenance_project').fetchone()[0]
    old = run(migrated, 'register', 'context', 1)['ref']
    replacement = run(migrated, 'register', 'context', 2)['ref']
    link = run(migrated, 'link', replacement, 'supersedes', old)
    with sqlite3.connect(migrated) as conn:
        conn.execute('DELETE FROM task_context_items WHERE id = 1')
        conn.execute("INSERT INTO task_context_items(id, task_id, item_type, content) VALUES (1, 1, 'memory', 'Reused ID')")
    assert run(migrated, 'get', old)['availability'] == 'unavailable'
    assert run(migrated, 'register', 'context', 1)['ref'] != old
    assert run(migrated, 'links', old)['links'] == [link]
    # Replaying a historical link stays idempotent after endpoint deletion.
    assert run(migrated, 'link', replacement, 'supersedes', old) == link
    run(migrated, 'link', replacement, 'supports', old, ok=False)
    external = run(migrated, 'register', 'artifact', '--key', 'v1')['ref']
    run(migrated, 'unavailable', external)
    assert run(migrated, 'get', external)['availability'] == 'unavailable'
    assert run(migrated, 'register', 'artifact', '--key', 'v1')['ref'] == external
    run(migrated, 'unavailable', replacement, ok=False)
    with sqlite3.connect(migrated) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        # Cascade deletion preserves dependent provenance tombstones too.
        # Existing session FKs deliberately restrict parent task deletion.
        conn.execute('DELETE FROM task_sessions WHERE task_id = 1')
        conn.execute('DELETE FROM tasks WHERE id = 1')
    assert run(migrated, 'get', replacement)['availability'] == 'unavailable'


def test_attribution_strength(db_path):
    seed(db_path)
    old = run(db_path, 'register', 'context', 1)['ref']
    new = run(db_path, 'register', 'context', 2)['ref']
    criterion = run(db_path, 'register', 'criterion', 1)['ref']
    # Same task, session, and timestamps do not synthesize causal links.
    assert counts(db_path) == (3, 0)
    run(db_path, 'link', new, 'supports', criterion, '--attribution', 'inferred', ok=False)
    run(db_path, 'link', new, 'supports', criterion, '--attribution', 'inferred', '--reason', '\t\n', ok=False)
    inferred = run(db_path, 'link', new, 'supports', criterion, '--attribution', 'inferred', '--reason', 'Operator hypothesis')
    explicit = run(db_path, 'link', new, 'supports', criterion)
    assert inferred['attribution'] == 'inferred' and inferred['reason'] == 'Operator hypothesis'
    assert explicit['attribution'] == 'explicit' and explicit['id'] != inferred['id']
    supersession = run(db_path, 'link', new, 'supersedes', old)
    assert supersession['source_ref'] == new and supersession['target_ref'] == old
    run(db_path, 'link', old, 'supersedes', new, ok=False)
    with sqlite3.connect(db_path) as conn:
        # Links describe relationships; they do not silently mutate domain status.
        assert conn.execute('SELECT status FROM task_context_items WHERE id = 1').fetchone()[0] == 'active'
        assert conn.execute('SELECT is_completed FROM acceptance_criteria WHERE id = 1').fetchone()[0] == 0


def test_schema_guards_and_concurrent_registration(db_path):
    seed(db_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: run(db_path, 'register', 'task', 1), range(4)))
    assert len({r['ref'] for r in records}) == 1
    rid = records[0]['id']
    run(db_path, 'register', 'session', 1)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO tasks(id, summary) VALUES (2, 'Unregistered task')")
        conn.commit()
        for sql, params in [
            ('DELETE FROM provenance_records WHERE id = ?', (rid,)),
            ('UPDATE provenance_records SET native_id = 99 WHERE id = ?', (rid,)),
            ('UPDATE tasks SET id = 99 WHERE id = 1', ()),
            ('UPDATE OR REPLACE tasks SET id = 1 WHERE id = 2', ()),
            ("UPDATE provenance_project SET project_id = 'wrong'", ()),
            ("INSERT OR REPLACE INTO provenance_project VALUES (1, 'wrong')", ()),
            ("INSERT OR REPLACE INTO tasks(id, summary) VALUES (1, 'Replaced')", ()),
            ("INSERT OR REPLACE INTO task_sessions(task_id, started_at) VALUES (1, datetime('now'))", ()),
            ("INSERT OR REPLACE INTO provenance_records(id, kind, native_id) VALUES (?, 'task', 1)", (rid,)),
            ("INSERT INTO provenance_records(id, kind, native_id) VALUES ('x', 'task', 99)", ()),
            ("INSERT INTO provenance_links(source_id, relationship, target_id) VALUES (?, 'verifies', 'missing')", (rid,)),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)
            conn.rollback()
    assert counts(db_path) == (2, 0)
