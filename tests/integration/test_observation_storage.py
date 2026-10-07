"""Observation storage preserves legacy capture, history, and guidance boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'bin/tusk'


def run(db, *args, ok=True):
    result = subprocess.run([str(CLI), *map(str, args)], cwd=ROOT,
                            env=dict(os.environ, TUSK_DB=str(db), TUSK_NO_BACKUP='1'),
                            capture_output=True, text=True, timeout=60)
    if not ok:
        assert result.returncode, result.stdout
        assert 'Traceback' not in result.stderr
        return result.stderr
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def migration():
    spec = importlib.util.spec_from_file_location('observation_migration', ROOT / 'bin/tusk-migrate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Actual pre-observation table definitions, including the old ownership FKs.
# Rebuild these rather than merely stamping a modern database as version 92.
LEGACY_DDL = """
CREATE TABLE task_context_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, objective_id INTEGER,
 item_type TEXT NOT NULL CHECK(item_type IN ('memory','assumption','question','risk','decision','entry_point')),
 content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','resolved','superseded')),
 source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','create_task','task_progress','review','retro','agent_handoff')),
 created_at TEXT NOT NULL DEFAULT(datetime('now')), updated_at TEXT NOT NULL DEFAULT(datetime('now')), resolved_at TEXT,
 FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
 FOREIGN KEY(objective_id) REFERENCES objectives(id) ON DELETE SET NULL);
CREATE INDEX idx_task_context_items_task_id ON task_context_items(task_id);
CREATE INDEX idx_task_context_items_objective_id ON task_context_items(objective_id);
CREATE INDEX idx_task_context_items_type_status ON task_context_items(item_type,status);
CREATE TABLE jots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, skill_run_id INTEGER NOT NULL, task_id INTEGER,
 category TEXT NOT NULL, note TEXT NOT NULL, file_hint TEXT, skill_hint TEXT,
 created_at TEXT NOT NULL DEFAULT(datetime('now')),
 FOREIGN KEY(skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE,
 FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL);
CREATE INDEX idx_jots_skill_run_id ON jots(skill_run_id);
CREATE INDEX idx_jots_task_id ON jots(task_id);
CREATE INDEX idx_jots_category ON jots(category);
"""


@pytest.fixture
def legacy(db_path, tmp_path):
    db = tmp_path / 'legacy92.db'
    with sqlite3.connect(db_path) as fresh, sqlite3.connect(db) as conn:
        fresh.backup(conn)
        # Remove only observation-owned DDL. Restore historical context/jot
        # provenance triggers from the frozen v89 schema below.
        for name, sql in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall():
            if any(token in (sql or '') for token in ('task_context_items', 'jot_aliases', 'jots')):
                conn.execute(f'DROP TRIGGER "{name}"')
        kind = conn.execute("SELECT type FROM sqlite_master WHERE name='jots'").fetchone()[0]
        conn.execute(f'DROP {kind.upper()} jots')
        conn.execute('DROP TABLE IF EXISTS jot_aliases')
        conn.execute('DROP TABLE task_context_items')
        conn.executescript(LEGACY_DDL)
        spec = importlib.util.spec_from_file_location('observation_old_provenance', ROOT / 'bin/tusk-provenance.py')
        provenance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provenance)
        # Extract complete trigger statements without splitting trigger bodies.
        statement = ''
        for line in provenance.schema_v89_sql().splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.lstrip().startswith(('CREATE TRIGGER provenance_context_', 'CREATE TRIGGER provenance_jot_')):
                    conn.executescript(statement)
                statement = ''
        conn.execute('PRAGMA user_version=92')
        conn.executescript("""
          INSERT INTO tasks(id,summary) VALUES(1,'Capture host'),(2,'Deleted host');
          INSERT INTO skill_runs(id,skill_name,task_id) VALUES(1,'tusk',1),(2,'retro',NULL);
          INSERT INTO task_context_items(id,task_id,item_type,content) VALUES(4,1,'memory','Unrelated same-ID context'),(50,1,'decision','Existing guidance');
          INSERT INTO task_context_items(id,task_id,item_type,content) VALUES(200,1,'memory','Deleted highest context');
          DELETE FROM task_context_items WHERE id=200;
          INSERT INTO jots(id,skill_run_id,task_id,category,note,file_hint,skill_hint,created_at) VALUES
            (4,1,1,'friction','Legacy owned note','bin/tusk','tusk','2026-01-01 01:02:03'),
            (7,2,NULL,'process','Taskless note',NULL,'retro','2026-01-02 01:02:03'),
            (9,2,2,'workflow','Deleted-task note','docs/DOMAIN.md',NULL,'2026-01-03 01:02:03'),
            (100,2,NULL,'discarded','Previously deleted note',NULL,NULL,'2026-01-04 01:02:03');
          DELETE FROM jots WHERE id=100;
        """)
        conn.commit()
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('DELETE FROM tasks WHERE id=2')
    return db


def test_migration_preserves_legacy_observations(legacy, db_path, config_path):
    with sqlite3.connect(legacy) as conn:
        before = conn.execute('SELECT * FROM jots ORDER BY id').fetchall()
    module = migration()
    module.migrate_93(str(legacy), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(legacy) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 93
        assert conn.execute('SELECT * FROM jots ORDER BY id').fetchall() == before
        assert conn.execute("SELECT type FROM sqlite_master WHERE name='jots'").fetchone()[0] == 'view'
        assert conn.execute("SELECT content FROM task_context_items WHERE id=50").fetchone()[0] == 'Existing guidance'
        assert conn.execute("SELECT count(*) FROM task_context_items WHERE item_type='observation' AND triage_status='pending'").fetchone()[0] == 3
        assert conn.execute("SELECT min(id) FROM task_context_items WHERE item_type='observation'").fetchone()[0] > 200
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        snapshot = list(conn.iterdump())
        relevant = "SELECT type,name,sql FROM sqlite_master WHERE name IN ('task_context_items','jot_aliases','jots') ORDER BY name"
        migrated_schema = conn.execute(relevant).fetchall()
    module.migrate_93(str(legacy), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(legacy) as conn, sqlite3.connect(db_path) as fresh:
        assert list(conn.iterdump()) == snapshot
        assert fresh.execute(relevant).fetchall() == migrated_schema
    new = run(legacy, 'jot', 'write', 'process', 'New after migration', '--skill-run-id', 2)
    assert new['id'] > 100  # Never reuse even a deleted legacy identity.


def test_legacy_provenance_survives(legacy, config_path):
    jot = run(legacy, 'provenance', 'register', 'jot', 4)
    context = run(legacy, 'provenance', 'register', 'context', 4)
    task = run(legacy, 'provenance', 'register', 'task', 1)
    link = run(legacy, 'provenance', 'link', task['ref'], 'derived_from', jot['ref'])
    migration().migrate_93(str(legacy), config_path, str(ROOT / 'bin'))
    assert run(legacy, 'provenance', 'get', jot['ref']) == jot
    assert run(legacy, 'provenance', 'get', context['ref']) == context
    assert run(legacy, 'provenance', 'links', jot['ref'])['links'] == [link]
    with sqlite3.connect(legacy) as conn:
        atom = conn.execute('SELECT context_id FROM jot_aliases WHERE id=4').fetchone()[0]
        conn.execute("UPDATE jots SET note='One authoritative note' WHERE id=4")
        assert conn.execute('SELECT content FROM task_context_items WHERE id=?', (atom,)).fetchone()[0] == 'One authoritative note'
        conn.execute("UPDATE task_context_items SET content='Edited atom' WHERE id=?", (atom,))
        assert conn.execute('SELECT note FROM jots WHERE id=4').fetchone()[0] == 'Edited atom'
        conn.commit()
        for sql in ('UPDATE jots SET id=40 WHERE id=4', 'UPDATE jot_aliases SET id=40 WHERE id=4',
                    'UPDATE jot_aliases SET context_id=50 WHERE id=4',
                    f'INSERT OR REPLACE INTO jot_aliases(id,context_id) VALUES(40,{atom})'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
            conn.rollback()
        conn.execute('DELETE FROM jots WHERE id=4')
        assert conn.execute('SELECT 1 FROM task_context_items WHERE id=?', (atom,)).fetchone() is None
    assert run(legacy, 'provenance', 'get', context['ref']) == context
    assert run(legacy, 'provenance', 'get', jot['ref'])['availability'] == 'unavailable'
    assert run(legacy, 'provenance', 'links', jot['ref'])['links'] == [link]


def test_capture_and_listing_compatibility(db_path):
    from tests.integration.test_jot_dispatch import _seed_parallel_runs
    first, first_run, second, second_run = _seed_parallel_runs(db_path)
    assert 'Ambiguous jot target' in run(db_path, 'jot', 'write', 'process', 'Ambiguous', ok=False)
    explicit = run(db_path, 'jot', 'write', 'process', 'Explicit capture', '--file', 'bin/tusk', '--skill', 'retro', '--task-id', first)
    shorthand = run(db_path, 'jot', 'workflow', 'Shorthand capture', '--skill-run-id', first_run)
    other = run(db_path, 'jot', 'write', 'process', 'Other capture', '--skill-run-id', second_run)
    assert explicit['file_hint'] == 'bin/tusk' and explicit['skill_hint'] == 'retro'
    assert explicit['task_id'] == first and explicit['skill_run_id'] == first_run
    assert shorthand['task_id'] == first and other['task_id'] == second
    assert [r['id'] for r in run(db_path, 'jots', '--task-id', first, '--skill-run-id', first_run)] == [shorthand['id'], explicit['id']]
    assert run(db_path, 'jots', '--task-id', first, '--skill-run-id', second_run) == []
    assert [r['id'] for r in run(db_path, 'jots', '--limit', 1)] == [other['id']]
    with sqlite3.connect(db_path) as conn:
        conn.execute('INSERT INTO task_workspaces(task_id,branch,workspace_path) VALUES(?,?,?)', (first, f'feature/TASK-{first}-capture', str(ROOT)))
    inferred = run(db_path, 'jot', 'write', 'process', 'Workspace capture')
    assert inferred['task_id'] == first and inferred['skill_run_id'] == first_run
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM task_context_items WHERE item_type='observation'").fetchone()[0] == 4


def test_pending_observations_excluded_from_guidance(db_path):
    from tests.integration.test_jot_dispatch import _seed_open_skill_run
    task = _seed_open_skill_run(db_path)
    objective = run(db_path, 'objective', 'insert', 'Observation guidance boundary')['id']
    run(db_path, 'objective', 'link', objective, task)
    decision = run(db_path, 'context', 'add', task, '--type', 'decision', '--content', 'Approved guidance', '--objective-id', objective)
    jot = run(db_path, 'jot', 'write', 'friction', 'UNTRIAGED SENTINEL')
    with sqlite3.connect(db_path) as conn:
        atom = conn.execute('SELECT context_id FROM jot_aliases WHERE id=?', (jot['id'],)).fetchone()[0]
        conn.execute('UPDATE task_context_items SET objective_id=? WHERE id=?', (objective, atom))
        assert conn.execute('SELECT status,triage_status FROM task_context_items WHERE id=?', (atom,)).fetchone() == ('active', 'pending')
    observations = run(db_path, 'context', 'list', task, '--type', 'observation')
    assert 'UNTRIAGED SENTINEL' in json.dumps(observations)
    assert 'UNTRIAGED SENTINEL' not in json.dumps(run(db_path, 'context', 'list', task))
    for args in (('task-brief', task), ('objective', 'brief', objective)):
        brief = json.dumps(run(db_path, *args))
        assert 'Approved guidance' in brief
        assert 'UNTRIAGED SENTINEL' not in brief
    run(db_path, 'context', 'resolve', atom, ok=False)
    run(db_path, 'context', 'supersede', atom, ok=False)
    run(db_path, 'context', 'resolve', decision['id'])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT status FROM task_context_items WHERE id=?', (decision['id'],)).fetchone()[0] == 'resolved'
        assert conn.execute('SELECT triage_status FROM task_context_items WHERE id=?', (atom,)).fetchone()[0] == 'pending'


def test_failed_migration_rolls_back_legacy_history(legacy, config_path):
    ref = run(legacy, 'provenance', 'register', 'jot', 4)
    with sqlite3.connect(legacy) as conn:
        # A damaged legacy FK must abort migration without partially replacing
        # tables, moving notes, or tombstoning an otherwise valid source ref.
        conn.execute('UPDATE jots SET skill_run_id=999 WHERE id=7')
        conn.commit()
        before = list(conn.iterdump())
    with pytest.raises((sqlite3.IntegrityError, RuntimeError, ValueError)):
        migration().migrate_93(str(legacy), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(legacy) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 92
    assert run(legacy, 'provenance', 'get', ref['ref']) == ref
