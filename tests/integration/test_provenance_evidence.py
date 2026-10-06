"""Immutable attempts preserve what ran, what was declared, and what was checked."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'bin/tusk'


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def run(db, repo, *args, ok=True, env_extra=None):
    env = dict(os.environ, TUSK_DB=str(db), TUSK_PROJECT=str(repo), TUSK_NO_BACKUP='1')
    env.update(env_extra or {})
    result = subprocess.run([str(CLI), *map(str, args)], cwd=repo, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert (result.returncode == 0) == ok, (result.stdout, result.stderr)
    assert 'Traceback' not in result.stderr, result.stderr
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout


@pytest.fixture
def project(db_path, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Evidence Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.test')
    (repo / '.gitignore').write_text('tusk/\n')
    (repo / 'code.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'Base')
    git(repo, 'checkout', '-b', 'feature/TASK-1-evidence')
    (repo / 'code.txt').write_text('first revision\n')
    git(repo, 'commit', '-am', '[TASK-1] First revision')
    (repo / 'tusk').mkdir()
    db = repo / 'tusk/tasks.db'
    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(db)
    source.backup(dest)
    source.close()
    dest.close()
    config = json.loads((ROOT / 'config.default.json').read_text())
    config['test_command'] = 'true'
    (repo / 'tusk/config.json').write_text(json.dumps(config))
    task = run(db, repo, 'task-insert', 'Evidence fixture', 'Persist checked targets', '--skip-dupe',
               '--typed-criteria', json.dumps({'text': 'Check passes', 'type': 'test', 'spec': 'false'}))
    return db, repo, task['criteria_ids'][0]


def history(db, repo, cid):
    return run(db, repo, 'provenance', 'evidence', '--criterion-id', cid)['evidence']


def test_verification_history(project):
    db, repo, cid = project
    run(db, repo, 'criteria', 'done', cid, ok=False)
    failed = history(db, repo, cid)[0]
    assert failed['evidence']['result']['outcome'] == 'failed'
    assert failed['evidence']['spec'] == 'false'
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    completed = run(db, repo, 'criteria', 'done', cid)
    assert completed['verification_contract']['evidence'] == 'executed'
    passed = history(db, repo, cid)[0]
    assert passed['evidence']['result']['automated'] == 1
    run(db, repo, 'criteria', 'reset', cid)
    run(db, repo, 'criteria', 'done', cid, '--skip-verify')
    run(db, repo, 'criteria', 'reset', cid)
    run(db, repo, 'criteria', 'done', cid, '--external-verification-url', 'https://ci.example.test/runs/7')
    run(db, repo, 'criteria', 'reset', cid)
    run(db, repo, 'criteria', 'done', cid, env_extra={
        'TUSK_COMMIT_GATE_VALIDATED': '1', 'TUSK_COMMIT_GATE_COMMAND': 'true',
        'TUSK_COMMIT_GATE_SHA': git(repo, 'rev-parse', 'HEAD'),
        'TUSK_COMMIT_GATE_EVIDENCE_REF': passed['ref'],
    })
    rows = history(db, repo, cid)
    assert len(rows) == 5 and len({r['ref'] for r in rows}) == 5
    assert {r['evidence']['mode'] for r in rows} == {'executed','bypassed','external','reused'}
    assert {r['evidence']['result']['outcome'] for r in rows} == {'failed','passed','bypassed','declared_passed'}
    for r in rows:
        assert r['evidence']['criterion_ref'] and r['evidence']['action_ref']
        assert r['evidence']['artifact_ref']
    assert run(db, repo, 'provenance', 'get', failed['ref']) == failed
    with sqlite3.connect(db) as conn:
        for table in ('provenance_attempts','provenance_results','provenance_artifacts'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                conn.execute(f'DELETE FROM {table}')


def test_revision_binding(project):
    db, repo, cid = project
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    run(db, repo, 'criteria', 'done', cid)
    original = history(db, repo, cid)[0]
    target = run(db, repo, 'provenance', 'get', original['evidence']['artifact_ref'])
    sha = git(repo, 'rev-parse', 'HEAD')
    assert target['artifact']['version'] == sha and len(sha) == 40
    review = run(db, repo, 'review', 'begin', 1)
    frozen = review['range']
    assert sha in frozen and 'main' not in frozen and 'HEAD' not in frozen
    (repo / 'code.txt').write_text('second revision\n')
    git(repo, 'commit', '-am', '[TASK-1] Second revision')
    # The legacy criterion attribution can refresh; historical evidence cannot.
    run(db, repo, 'criteria', 'done', cid)
    assert history(db, repo, cid) == [original]
    run(db, repo, 'review', 'approve', review['review_id'], '--skip-cost', '--model', 'fixture')
    all_rows = run(db, repo, 'provenance', 'evidence')['evidence']
    review_proof = next(r for r in all_rows if r['evidence']['mode'] == 'review')
    review_target = run(db, repo, 'provenance', 'get', review_proof['evidence']['artifact_ref'])
    assert review_target['artifact']['version'] == frozen
    assert review_proof['evidence']['result']['automated'] == 0
    run(db, repo, 'criteria', 'reset', cid)
    # Modifying checked source during the command must not verify its old target.
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'printf changed > code.txt')
    run(db, repo, 'criteria', 'done', cid)
    changed = history(db, repo, cid)[0]['evidence']
    assert changed['result']['outcome'] == 'passed'
    assert changed['result']['target_matches'] == 0 and changed['result']['automated'] == 0


def test_external_artifacts(project):
    db, repo, cid = project
    for kind in ('document','deployment','external_run'):
        args = ('provenance','capture-artifact','--kind',kind,'--uri',f'https://example.test/{kind}', '--version','v1')
        first = run(db, repo, *args)
        assert run(db, repo, *args) == first
        second = run(db, repo, *args[:-1], 'v2')
        assert second['ref'] != first['ref']
        declared = run(db, repo, 'provenance', 'declare-evidence', '--criterion-id', cid,
                       '--artifact', first['ref'], '--source-uri', 'https://ci.example.test/claim', '--outcome', 'passed')
        assert declared['evidence']['result']['outcome'] == 'declared_passed'
        assert declared['evidence']['result']['automated'] == 0
        assert not any(e['relationship'] == 'verifies' for e in run(db, repo, 'provenance', 'links', declared['ref'])['links'])
    missing = run(db, repo, 'provenance', 'capture-artifact', '--kind', 'document', '--uri', 'file:///gone', '--unavailable')
    assert missing['availability'] == 'unavailable' and not missing['artifact']['version_known']
    declared = run(db, repo, 'provenance', 'declare-evidence', '--artifact', missing['ref'],
                   '--source-uri', 'operator:manual', '--outcome', 'failed')
    assert declared['evidence']['result']['outcome'] == 'declared_failed'


def test_existing_verification_contract(project, monkeypatch):
    db, repo, cid = project
    skipped = run(db, repo, 'criteria', 'done', cid, '--skip-verify')
    assert skipped['verification_contract']['strength'] == 'bypassed'
    assert history(db, repo, cid)[0]['evidence']['result']['automated'] == 0
    run(db, repo, 'criteria', 'reset', cid)
    external = run(db, repo, 'criteria', 'done', cid, '--external-verification-url', 'https://ci.example.test/legacy')
    assert external['verification_contract']['evidence'] == 'external_verification'
    assert history(db, repo, cid)[0]['evidence']['result']['automated'] == 0
    sys.path.insert(0, str(ROOT / 'bin'))
    import tusk_loader
    evidence = tusk_loader.load('tusk-evidence-lib')
    actions = tusk_loader.load('tusk-action-lib')
    action = actions.CLIAction('interrupted verifier')
    conn = action.get_connection(str(db))
    ref = evidence.start(conn, action, task_id=1, criterion_id=cid, spec='unobserved command', target={'state':'unknown'})
    conn.close()
    pending = run(db, repo, 'provenance', 'get', ref)['evidence']
    assert pending['state'] == 'pending' and pending['result'] is None and pending['artifact_ref'] is None
    # Real commit gate capture, followed by same-content reuse at a new commit.
    gate = evidence.begin_gate(str(db), 1, str(repo), 'true')
    evidence.finish_gate(gate, subprocess.CompletedProcess(['true'], 0, '', ''), True)
    run(db, repo, 'criteria', 'reset', cid)
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    reused = run(db, repo, 'criteria', 'done', cid, env_extra={
        'TUSK_COMMIT_GATE_VALIDATED':'1', 'TUSK_COMMIT_GATE_COMMAND':'true',
        'TUSK_COMMIT_GATE_SHA':git(repo,'rev-parse','HEAD'), 'TUSK_COMMIT_GATE_EVIDENCE_REF':gate['ref'],
    })
    assert reused['verification_contract']['evidence'] == 'reused_commit_gate'
    assert history(db, repo, cid)[0]['evidence']['result']['automated'] == 1
    (repo / 'code.txt').write_text('changed after the gate\n')
    run(db, repo, 'criteria', 'reset', cid)
    run(db, repo, 'criteria', 'done', cid, env_extra={
        'TUSK_COMMIT_GATE_VALIDATED':'1', 'TUSK_COMMIT_GATE_COMMAND':'true',
        'TUSK_COMMIT_GATE_SHA':git(repo,'rev-parse','HEAD'), 'TUSK_COMMIT_GATE_EVIDENCE_REF':gate['ref'],
    })
    assert history(db, repo, cid)[0]['evidence']['result']['automated'] == 0


def test_commit_gate_pipeline(project):
    db, repo, cid = project
    run(db, repo, 'task-start', 1, '--force')
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    (repo / 'code.txt').write_text('checked by commit gate\n')
    run(db, repo, 'scope', 'add', 1, 'code.txt')
    run(db, repo, 'commit', 1, 'Commit checked content', 'code.txt', '--criteria', cid)
    row = history(db, repo, cid)[0]['evidence']
    assert row['mode'] == 'reused' and row['result']['automated'] == 1
    target = run(db, repo, 'provenance', 'get', row['artifact_ref'])['artifact']
    assert target['version'] == git(repo, 'rev-parse', 'HEAD')
    source = run(db, repo, 'provenance', 'get', row['source_ref'])['evidence']
    assert source['mode'] == 'executed' and source['result']['automated'] == 1
    assert source['details']['target']['digest'] == row['details']['target']['digest']
    assert source['details']['target']['head'] != row['details']['target']['head']


def test_evidence_migration(db_path, config_path):
    sys.path.insert(0, str(ROOT / 'bin'))
    import tusk_loader
    migration = tusk_loader.load('tusk-migrate')
    with sqlite3.connect(db_path) as conn:
        objects = "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'provenance_artifact%' OR name LIKE 'provenance_attempt%' OR name LIKE 'provenance_result%' OR name LIKE 'provenance_review_target%' OR name LIKE 'idx_provenance_attempt%' ORDER BY name"
        expected = conn.execute(objects).fetchall()
        for name in ('provenance_results','provenance_attempts','provenance_review_targets','provenance_artifacts'):
            conn.execute(f'DROP TABLE {name}')
        conn.execute('PRAGMA user_version=91')
    migration.migrate_92(str(db_path), config_path, str(ROOT / 'bin'))
    migration.migrate_92(str(db_path), config_path, str(ROOT / 'bin'))
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 92
        assert conn.execute(objects).fetchall() == expected


def test_gate_timeout_history(project, monkeypatch):
    db, repo, cid = project
    sys.path.insert(0, str(ROOT / 'bin'))
    import tusk_loader
    evidence = tusk_loader.load('tusk-evidence-lib')
    commit = tusk_loader.load('tusk-commit')
    handle = evidence.begin_gate(str(db), 1, str(repo), 'fixture-test')
    original_ref = handle['ref']
    real_run = subprocess.run
    calls = []
    def progressing_then_pass(*args, **kwargs):
        if kwargs.get('shell'):
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('fixture-test', 1, output=b'progress\n')
            return subprocess.CompletedProcess('fixture-test', 0, 'passed', '')
        return real_run(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', progressing_then_pass)
    result, _ = commit._run_test_with_retry('fixture-test', str(repo), 1, 'auto', False, gate_evidence=handle)
    evidence.finish_gate(handle, result, True)
    assert len(calls) == 2 and handle['ref'] != original_ref
    old = run(db, repo, 'provenance', 'get', original_ref)['evidence']
    new = run(db, repo, 'provenance', 'get', handle['ref'])['evidence']
    assert old['result']['outcome'] == 'unknown' and old['result']['automated'] == 0
    assert new['result']['outcome'] == 'passed' and new['result']['automated'] == 1
