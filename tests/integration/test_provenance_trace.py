"""Public trace queries stay bounded, deterministic, and independent of chronology."""
import sqlite3

from tests.integration.test_provenance_evidence import project, run, git


def register(db, repo, kind, identity=None, **kwargs):
    args = ['provenance', 'register', kind]
    if identity is not None:
        args.append(identity)
    for key, value in kwargs.items():
        args.extend(['--' + key, value])
    return run(db, repo, *args)['ref']


def link(db, repo, a, relationship, b, *flags):
    return run(db, repo, 'provenance', 'link', a, relationship, b, *flags)


def refs(data):
    return {n['ref'] for n in data['nodes']}


def test_trace_directions(project, tmp_path):
    db, repo, cid = project
    path = tmp_path / 'source.txt'
    path.write_text('Keep the reason behind each revision.')
    prompt = run(db, repo, 'provenance', 'capture-prompt', '--file', path, '--representation', 'excerpt')['ref']
    criterion = register(db, repo, 'criterion', cid)
    task = register(db, repo, 'task', 1)
    link(db, repo, task, 'derived_from', prompt)
    link(db, repo, criterion, 'derived_from', prompt, '--attribution', 'inferred', '--reason', 'Selected intent')
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    proof = run(db, repo, 'criteria', 'done', cid)['evidence_ref']
    evidence = run(db, repo, 'provenance', 'get', proof)['evidence']
    artifact = evidence['artifact_ref']
    link(db, repo, artifact, 'implements', criterion)
    decision = run(db, repo, 'context', 'add', 1, '--type', 'assumption', '--content', 'Original basis')
    decision_ref = register(db, repo, 'context', decision['id'])
    link(db, repo, decision_ref, 'supports', criterion)
    changed_basis = run(db, repo, 'trace', decision_ref, '--direction', 'dependents', '--depth', 5)
    assert {criterion, artifact, proof} <= refs(changed_basis)
    ancestors = run(db, repo, 'trace', proof, '--depth', 5)
    assert {proof, artifact, criterion, prompt, decision_ref, evidence['action_ref']} <= refs(ancestors)
    assert task not in refs(ancestors)
    revision = next(n for n in ancestors['nodes'] if n['ref'] == artifact)
    assert revision['details']['version'] == git(repo, 'rev-parse', 'HEAD')
    assert any(e['attribution'] == 'inferred' and e['reason'] == 'Selected intent' for e in ancestors['links'])
    dependents = run(db, repo, 'trace', prompt, '--direction', 'dependents', '--depth', 5)
    assert {task, criterion, proof, artifact} <= refs(dependents)
    human = run(db, repo, 'trace', proof, '--format', 'text', '--depth', 5)
    for text in ('derived_from', 'inferred', 'Selected intent', 'verifies', git(repo, 'rev-parse', 'HEAD')):
        assert text in human
    assert run(db, repo, 'trace', prompt, '--direction', 'both', '--depth', 5)['scope'] == 'recorded_links_only'


def test_bounded_traversal(project):
    db, repo, _ = project
    a, b, c, d = [register(db, repo, 'prompt', key=k) for k in 'abcd']
    for left, right in ((a,b),(a,c),(b,d),(c,d),(d,a)):
        link(db, repo, left, 'derived_from', right)
    complete = run(db, repo, 'trace', a, '--depth', 10)
    assert len(complete['nodes']) == 4 and len(complete['links']) == 5
    assert not complete['truncated']
    assert run(db, repo, 'trace', a, '--depth', 10) == complete
    limited = run(db, repo, 'trace', a, '--limit', 2)
    assert len(limited['nodes']) <= 2 and len(limited['links']) <= 2
    assert limited['truncated'] and 'limit' in limited['truncation_reasons']
    shallow = run(db, repo, 'trace', a, '--depth', 0)
    assert refs(shallow) == {a} and shallow['truncation_reasons'] == ['depth']
    run(db, repo, 'provenance', 'unavailable', d)
    assert next(n for n in run(db, repo, 'trace', a)['nodes'] if n['ref'] == d)['availability'] == 'unavailable'
    context = run(db, repo, 'context', 'add', 1, '--type', 'decision', '--content', 'Retained context')
    native = register(db, repo, 'context', context['id'])
    link(db, repo, native, 'derived_from', a)
    # Exercise a native tombstone via an isolated database deletion.
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM task_context_items WHERE id=?', (context['id'],))
    assert run(db, repo, 'trace', native)['nodes'][0]['availability'] == 'unavailable'
    run(db, repo, 'trace', a.rsplit(':', 1)[0] + ':' + '0' * 32, ok=False)
    run(db, repo, 'trace', 'tusk:' + '0' * 32 + ':' + a.rsplit(':', 1)[1], ok=False)
    for flag, value in (('--limit',0),('--limit',1001),('--depth',-1),('--depth',21)):
        run(db, repo, 'trace', a, flag, value, ok=False)


def test_causal_not_chronological(project):
    db, repo, _ = project
    started = run(db, repo, 'task-start', 1, '--force')
    ids = []
    for content in ('Old choice','New choice','Unrelated choice'):
        atom = run(db, repo, 'context', 'add', 1, '--type', 'decision', '--content', content,
                   env_extra={'TUSK_ACTION_SESSION_ID': str(started['session_id'])})
        ids.append((atom['id'], register(db, repo, 'context', atom['id'])))
    link(db, repo, ids[1][1], 'supersedes', ids[0][1])
    run(db, repo, 'context', 'supersede', ids[0][0])
    with sqlite3.connect(db) as conn:
        counts = conn.execute('SELECT COUNT(*) FROM provenance_records').fetchone()[0]
    data = run(db, repo, 'trace', ids[1][1])
    assert refs(data) == {ids[0][1], ids[1][1]}
    assert {n['details']['status'] for n in data['nodes']} == {'active', 'superseded'}
    assert data['links'][0]['relationship'] == 'supersedes'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM provenance_records').fetchone()[0] == counts
    long = run(db, repo, 'context', 'add', 1, '--type', 'memory', '--content', 'x' * 2000)
    clipped = run(db, repo, 'trace', register(db, repo, 'context', long['id']))['nodes'][0]
    assert clipped['content_truncated'] and len(clipped['details']['content']) == 401
