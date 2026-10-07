"""Capture/triage/proposal behavior against the installed schema and public CLI."""
import sqlite3

import pytest

from tests.integration.test_observation_storage import run


@pytest.fixture
def capture(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            INSERT INTO tasks(id,summary) VALUES(1,'Friction host'),(2,'Approved follow-up');
            INSERT INTO skill_runs(id,skill_name,task_id) VALUES(1,'tusk',1);
            INSERT INTO task_context_items(task_id,item_type,content)
            VALUES(1,'memory','Ordinary guidance is not a friction occurrence');
        """)
    def write(note, category='workflow'):
        return run(db_path, 'jot', 'write', category, note, '--skill-run-id', 1)
    return write


def proposals(db):
    return run(db, 'propose-work', '--no-todo-scan')


def friction(db):
    return [p for p in proposals(db) if p['source'] == 'jot_category']


def dismiss(db, jot):
    return run(db, 'jot', 'dismiss', jot['id'], '--reason', 'Already handled')


def promote(db, jot):
    target = run(db, 'provenance', 'register', 'task', 2)['ref']
    return run(db, 'jot', 'promote', jot['id'], '--to', target)


def test_only_pending_observations_count(db_path, capture):
    first = capture('Older pending evidence')
    latest = capture('Latest pending evidence')
    dismissed = capture('DISMISSED SAMPLE MUST NOT APPEAR')
    promoted = capture('PROMOTED SAMPLE MUST NOT APPEAR')
    # Equal timestamps prove the context identity provides a stable tie-break.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE task_context_items SET created_at='2026-01-01' WHERE item_type='observation'")
    dismiss(db_path, dismissed)
    promote(db_path, promoted)
    candidate, = friction(db_path)
    assert candidate['evidence']['count'] == 2
    assert candidate['score'] == 55.0
    assert 'Latest pending evidence' in candidate['detail']
    assert 'MUST NOT APPEAR' not in candidate['detail']
    assert {r['jot_id'] for r in candidate['evidence']['contributors']} == {first['id'], latest['id']}
    dismiss(db_path, first)
    assert friction(db_path) == []  # One pending occurrence is below the old floor.
    dismiss(db_path, latest)
    assert friction(db_path) == []  # Addressed history alone cannot resurrect it.


def test_new_recurrence_and_other_sources(db_path, capture):
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            INSERT INTO task_progress(task_id,next_steps) VALUES(1,'Resume unrelated handoff');
            INSERT INTO retro_findings(skill_run_id,task_id,category,summary,action_taken)
            VALUES(1,1,'C','Validate existing patch','skill-patch:skills/example/SKILL.md');
            INSERT INTO task_sessions(id,task_id,started_at) VALUES(1,1,datetime('now'));
            INSERT INTO tool_call_stats(session_id,tool_name,call_count,total_cost)
            VALUES(1,'Read',90,1.5);
        """)
    others = proposals(db_path)
    assert {p['source'] for p in others} == {'skill_patch', 'next_steps', 'cost_outlier'}
    old = [capture('Old occurrence one'), capture('Old occurrence two')]
    for jot in old:
        dismiss(db_path, jot)
    capture('New occurrence one')
    assert friction(db_path) == []
    capture('New occurrence two')
    candidate, = friction(db_path)
    assert candidate['score'] == 55.0 and candidate['evidence']['count'] == 2
    capture('New occurrence three')
    all_proposals = proposals(db_path)
    candidate, = [p for p in all_proposals if p['source'] == 'jot_category']
    assert candidate['score'] == 60.0 and candidate['evidence']['count'] == 3
    assert [p for p in all_proposals if p['source'] != 'jot_category'] == others
    assert all_proposals == sorted(all_proposals, key=lambda p: (-p['score'], p['source'], p['title']))


def test_contributor_provenance(db_path, capture):
    captures = [capture('Same category first'), capture('Same category second')]
    with sqlite3.connect(db_path) as conn:
        identities = conn.execute('SELECT context_id,id FROM jot_aliases ORDER BY context_id DESC').fetchall()
        assert all(context_id != jot_id for context_id, jot_id in identities)
        assert conn.execute("SELECT count(*) FROM provenance_records WHERE kind IN ('context','jot')").fetchone()[0] == 0
        before = list(conn.iterdump())
    first = proposals(db_path)
    assert proposals(db_path) == first
    candidate, = [p for p in first if p['source'] == 'jot_category']
    assert candidate['evidence']['contributors'] == [
        {'kind': 'context', 'native_id': context_id, 'jot_id': jot_id}
        for context_id, jot_id in identities
    ]
    assert candidate['evidence']['count'] == len(captures)  # Aliases are not extra observations.
    atoms = run(db_path, 'context', 'list', 1, '--type', 'observation')
    assert {a['id'] for a in atoms} == {r['native_id'] for r in candidate['evidence']['contributors']}
    with sqlite3.connect(db_path) as conn:
        assert list(conn.iterdump()) == before  # Even provenance registration is forbidden here.
