"""Advisory checkpoint policy, delivery deduplication and task isolation."""
import fcntl
from pathlib import Path

import pytest

from tests.unit.test_codex_task_context_hooks import pickup


@pytest.fixture
def reminder(pickup, monkeypatch):
    p = pickup
    monkeypatch.delenv('TUSK_CODEX_CHECKPOINTS', raising=False)
    p.conn.execute('ALTER TABLE task_sessions ADD COLUMN started_at TEXT')
    p.conn.execute("UPDATE task_sessions SET started_at=datetime('now', '-1 hour')")
    p.conn.commit()
    return p


@pytest.mark.parametrize('event', ['Stop', 'Interrupt'])
def test_contract_1(reminder, event):
    p = reminder
    invoke = lambda turn: p.invoke(hook_event_name=event, turn_id=turn)
    message = invoke('initial')['systemMessage']
    assert 'TASK-42' in message and 'tusk progress 42 --next-steps' in message
    assert '99' not in message and len(message) < 400
    p.conn.execute("UPDATE task_progress SET next_steps='useful recovery action', created_at=datetime('now') WHERE task_id=42")
    p.conn.commit()
    assert invoke('checkpointed') == {}
    p.conn.execute("UPDATE task_progress SET created_at=datetime('now', '-31 minutes') WHERE task_id=42")
    p.conn.commit()
    assert 'systemMessage' in invoke('stale')
    p.conn.execute("UPDATE task_progress SET next_steps=char(9)||char(10)||' ', created_at=datetime('now') WHERE task_id=42")
    p.conn.commit()
    assert 'systemMessage' in invoke('blank')
    p.conn.execute('UPDATE acceptance_criteria SET is_completed=1 WHERE task_id=42')
    p.conn.commit()
    assert invoke('finished') == {}
    p.conn.execute('UPDATE acceptance_criteria SET is_completed=0 WHERE task_id=42')
    p.conn.execute("UPDATE tasks SET status='Done' WHERE id=42")
    p.conn.commit()
    assert invoke('closed') == {}


def test_contract_2(reminder):
    p = reminder
    before = list(p.conn.iterdump())
    for event in ('Stop', 'Interrupt'):
        result = p.invoke(hook_event_name=event, turn_id=event)
        assert set(result) == {'systemMessage'}
        assert 'Stopping is respected' in result['systemMessage']
        assert list(p.conn.iterdump()) == before
    assert p.invoke(hook_event_name='Stop', turn_id='recursive', stop_hook_active=True) == {}
    # A checkpoint in a previous session is not a recovery note for this session.
    p.conn.execute("UPDATE task_sessions SET started_at=datetime('now') WHERE task_id=42")
    p.conn.execute("UPDATE task_progress SET next_steps='old session', created_at=datetime('now', '-1 minute') WHERE task_id=42")
    p.conn.commit()
    assert 'systemMessage' in p.invoke(hook_event_name='Interrupt', turn_id='new-session')


def test_contract_3(reminder, monkeypatch, capsys):
    p = reminder
    invoke = lambda event='Stop', **kw: p.invoke(hook_event_name=event, **kw)
    assert 'systemMessage' in invoke(turn_id='same')
    assert invoke(turn_id='same') == {} and invoke('Interrupt', turn_id='same') == {}
    assert 'systemMessage' in invoke(turn_id='next')
    assert invoke() == {} and invoke(turn_id=[]) == {}
    monkeypatch.chdir(p.primary)
    assert p.invoke(hook_event_name='Stop', cwd=p.primary, turn_id='no-task') == {}
    monkeypatch.chdir(p.second)
    assert 'TASK-99' in p.invoke(hook_event_name='Stop', cwd=p.second, turn_id='same')['systemMessage']
    monkeypatch.chdir(p.first)
    p.conn.execute('INSERT INTO task_sessions VALUES (44, 42, NULL, datetime(\'now\'))')
    p.conn.commit()
    assert invoke(turn_id='ambiguous') == {}
    p.conn.execute('DELETE FROM task_sessions WHERE id=44')
    p.conn.commit()
    # An overlapping invocation cannot claim the same delivery cache.
    context = p.hooks.resolve_context({'cwd': str(p.first), 'session_id': 'thr_codex'})
    path = Path(context['db_path'] + '-hook-state') / 'codex-checkpoints/43.json'
    with path.open('r+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert invoke(turn_id='busy') == {}
    assert 'systemMessage' in invoke(turn_id='busy')
    # New Codex sessions do not inherit old turn delivery receipts.
    assert 'systemMessage' in invoke(turn_id='same', session_id='thr_new')
    definitions = p.hooks.definitions()
    assert all(definitions[e][0]['hooks'][0]['timeout'] == 3 for e in ('Stop', 'Interrupt'))
    # Cache storage and DB errors must remain advisory.
    path.write_text('broken')
    assert invoke(turn_id='bad-cache') == {}
    p.conn.execute('DROP TABLE task_progress')
    p.conn.commit()
    assert invoke(turn_id='missing-schema') == {}
    monkeypatch.setenv('TUSK_CODEX_CHECKPOINTS', '0')
    monkeypatch.setattr(p.hooks, 'resolve_context', lambda _: pytest.fail('opt-out must skip reads'))
    assert invoke(turn_id='disabled') == {}
    assert capsys.readouterr() == ('', '')
