"""Current provenance is selective, bounded, and conservative about proof."""
import json
import sqlite3

from tests.integration.test_provenance_evidence import project, run


def register(db, repo, kind, identity):
    return run(db, repo, 'provenance', 'register', kind, identity)['ref']


def test_current_context_selection(project, tmp_path):
    db, repo, cid = project
    path = tmp_path / 'request'
    path.write_text('Keep intent when resuming.')
    prompt = run(db, repo, 'provenance', 'capture-prompt', '--file', path, '--representation', 'excerpt')
    task = register(db, repo, 'task', 1)
    run(db, repo, 'provenance', 'link', task, 'derived_from', prompt['ref'])
    old = run(db, repo, 'context', 'add', 1, '--type', 'decision', '--content', 'Obsolete instruction')
    old_ref = register(db, repo, 'context', old['id'])
    run(db, repo, 'context', 'supersede', old['id'])
    run(db, repo, 'context', 'add', 1, '--type', 'decision', '--content', 'Current instruction')
    run(db, repo, 'context', 'add', 1, '--type', 'question', '--content', 'Which retention period?')
    run(db, repo, 'progress', 1, '--next-steps', 'Finish the remaining promise')
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    run(db, repo, 'criteria', 'done', cid)
    run(db, repo, 'criteria', 'add', 1, 'Remaining promise')
    packet = run(db, repo, 'task-brief', 1)['provenance']
    assert {'source_intent','decision','question','next_steps','evidence','outstanding_promise','historical_context'} <= {i['type'] for i in packet['items']}
    assert 'Obsolete instruction' not in json.dumps(packet)
    assert any(i['ref'] == old_ref for i in packet['items'])
    assert any(i.get('automated_current') for i in packet['items'])
    markdown = run(db, repo, 'task-brief', 1, '--format', 'markdown')
    assert 'Current Provenance' in markdown and 'Current instruction' in markdown


def test_budgeted_hydration(project, tmp_path):
    db, repo, _ = project
    for i in range(25):
        run(db, repo, 'context', 'add', 1, '--type', 'decision', '--content', f'Decision {i}: ' + 'x' * 1000)
    other = run(db, repo, 'task-insert', 'Unrelated task', 'Different intent', '--criteria', 'Other promise', '--skip-dupe')['task_id']
    run(db, repo, 'context', 'add', other, '--type', 'decision', '--content', 'UNRELATED SENTINEL')
    brief = run(db, repo, 'task-brief', 1, '--provenance-budget', 2000)
    packet = brief['provenance']
    assert len(json.dumps(packet, ensure_ascii=True, separators=(',', ':'))) <= 2000
    assert packet['truncated'] and packet['selection_truncated'] and packet['omitted_refs']
    assert 'UNRELATED SENTINEL' not in json.dumps(brief)
    assert run(db, repo, 'task-brief', 1, '--provenance-budget', 2000) == brief
    run(db, repo, 'task-brief', 1, '--provenance-budget', 1999, ok=False)


def test_legacy_and_revision_safety(project, tmp_path):
    db, repo, cid = project
    legacy = run(db, repo, 'task-brief', 1)
    assert legacy['task']['summary'] and legacy['acceptance_criteria']
    assert not any(i['type'] == 'source_intent' for i in legacy['provenance']['items'])
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    run(db, repo, 'criteria', 'done', cid)
    def proof():
        return next(i for i in run(db, repo, 'task-brief', 1)['provenance']['items'] if i['type'] == 'evidence')
    assert proof()['state'] == 'current'
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'false')
    assert proof()['state'] == 'stale'
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    (repo / 'code.txt').write_text('Changed after verification\n')
    assert proof()['state'] == 'stale' and not proof()['automated_current']
    run(db, repo, 'criteria', 'reset', cid)
    run(db, repo, 'criteria', 'done', cid, '--skip-verify')
    assert proof()['state'] == 'bypassed'
    path = tmp_path / 'source'
    path.write_text('Unavailable intent must not be active guidance')
    source = run(db, repo, 'provenance', 'capture-prompt', '--file', path, '--representation', 'excerpt')['ref']
    run(db, repo, 'provenance', 'link', register(db, repo, 'task', 1), 'derived_from', source)
    run(db, repo, 'provenance', 'unavailable', source)
    with sqlite3.connect(db) as conn:
        before = '\n'.join(conn.iterdump())
    packet = run(db, repo, 'task-brief', 1)['provenance']
    intent = next(i for i in packet['items'] if i['type'] == 'source_intent')
    assert intent['state'] == 'unknown' and intent['text'] is None and intent['ref'] == source
    with sqlite3.connect(db) as conn:
        assert '\n'.join(conn.iterdump()) == before
