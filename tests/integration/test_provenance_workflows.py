"""Exercise the skill handoff using real CLI writes and transcript-free reads."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import pytest

from tests.integration.test_provenance_evidence import project, git, run, ROOT, CLI


@pytest.fixture(autouse=True)
def isolated_attribution(monkeypatch, tmp_path):
    for name in os.environ:
        if name.startswith('TUSK_ACTION_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('TUSK_STATE_DIR', str(tmp_path / 'state'))


def ref(db, repo, kind, native_id):
    return run(db, repo, 'provenance', 'register', kind, native_id)['ref']


def link(db, repo, source, relationship, target):
    return run(db, repo, 'provenance', 'link', source, relationship, target)


def edges(db, repo, source):
    return run(db, repo, 'provenance', 'links', source, '--direction', 'outgoing', '--limit', 20)['links']


def capture(db, repo, path, content):
    path.write_text(content, encoding='utf-8')
    return run(db, repo, 'provenance', 'capture-prompt', '--file', path,
               '--representation', 'excerpt')


@pytest.fixture
def workflow(db_path, tmp_path):
    repo = tmp_path / 'workflow'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Workflow Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.test')
    (repo / '.gitignore').write_text('tusk/\n')
    (repo / 'code.txt').write_text('Base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'Base')
    git(repo, 'checkout', '-b', 'feature/TASK-1-workflow')
    (repo / 'tusk').mkdir()
    db = repo / 'tusk/tasks.db'
    with sqlite3.connect(db_path) as source, sqlite3.connect(db) as target:
        source.backup(target)
    config = json.loads((ROOT / 'config.default.json').read_text())
    config['test_command'] = 'true'
    (repo / 'tusk/config.json').write_text(json.dumps(config))
    source_file = tmp_path / 'selected-transcript.txt'
    prompt = capture(db, repo, source_file, 'Keep one durable decision and verify the delivered revision.')
    unrelated = capture(db, repo, tmp_path / 'other.txt', 'An unrelated task needs a blue button.')
    created = run(db, repo, 'task-insert', 'Retain decisions', 'Keep decisions and verify revisions.',
                  '--skip-dupe', '--typed-criteria',
                  json.dumps({'text': 'Revision is checked', 'type': 'test', 'spec': 'true'}),
                  env_extra={'TUSK_ACTION_SOURCE_REF': prompt['ref']})
    assert created['task_id'] == 1
    cid = created['criteria_ids'][0]
    task_ref, criterion_ref = ref(db, repo, 'task', 1), ref(db, repo, 'criterion', cid)
    link(db, repo, task_ref, 'derived_from', prompt['ref'])
    link(db, repo, criterion_ref, 'derived_from', prompt['ref'])
    started = run(db, repo, 'task-start', 1, '--force', '--skill', 'tusk')
    context_env = {'TUSK_ACTION_TASK_ID': '1',
                   'TUSK_ACTION_SESSION_ID': str(started['session_id']),
                   'TUSK_ACTION_SKILL_RUN_ID': str(started['skill_run']['run_id']),
                   'TUSK_ACTION_SOURCE_REF': prompt['ref']}
    decision = run(db, repo, 'context', 'add', 1, '--type', 'decision', '--source', 'create_task',
                   '--content', 'Keep only the latest decision.', env_extra=context_env)
    old_ref = ref(db, repo, 'context', decision['id'])
    link(db, repo, old_ref, 'derived_from', prompt['ref'])
    link(db, repo, old_ref, 'supports', criterion_ref)
    question = run(db, repo, 'context', 'add', 1, '--type', 'question',
                   '--content', 'How long should historical revisions be retained?')
    run(db, repo, 'criteria', 'update', cid, '--verification-spec', 'true')
    (repo / 'code.txt').write_text('Durable revision\n')
    run(db, repo, 'scope', 'add', 1, 'code.txt')
    run(db, repo, 'commit', 1, 'Persist revision', 'code.txt', '--criteria', cid)
    review = run(db, repo, 'review', 'begin', 1)
    run(db, repo, 'review', 'add-comment', review['review_id'], 'Preserve superseded decisions for audit.',
        '--category', 'suggest', '--severity', 'minor')
    finding = run(db, repo, 'review', 'list', 1)[0]['comments'][0]
    finding_ref = ref(db, repo, 'finding', finding['id'])
    replacement = run(db, repo, 'context', 'add', 1, '--type', 'decision', '--source', 'review',
                      '--content', 'Keep history; read only active decisions.',
                      env_extra={'TUSK_ACTION_SOURCE_REF': finding_ref})
    new_ref = ref(db, repo, 'context', replacement['id'])
    link(db, repo, new_ref, 'responds_to', finding_ref)
    link(db, repo, new_ref, 'supports', criterion_ref)
    link(db, repo, new_ref, 'supersedes', old_ref)
    run(db, repo, 'context', 'supersede', decision['id'])
    followup = run(db, repo, 'task-insert', 'Define retention policy', 'Decide how long history is retained.',
                   '--criteria', 'Retention duration documented', '--skip-dupe',
                   env_extra={'TUSK_ACTION_SOURCE_REF': finding_ref})
    followup_ref = ref(db, repo, 'task', followup['task_id'])
    link(db, repo, followup_ref, 'responds_to', finding_ref)
    run(db, repo, 'review', 'resolve', finding['id'], 'dismissed',
        '--note', f'Preserved as context {replacement["id"]}; tracked as TASK-{followup["task_id"]}.')
    run(db, repo, 'review', 'approve', review['review_id'], '--skip-cost', '--model', 'fixture')
    proof = run(db, repo, 'provenance', 'evidence', '--criterion-id', cid)['evidence'][0]
    artifact_ref = proof['evidence']['artifact_ref']
    link(db, repo, artifact_ref, 'implements', criterion_ref)
    run(db, repo, 'progress', 1, '--next-steps', f'Inspect source {prompt["ref"]} and proof {proof["ref"]}.')
    source_file.unlink()  # no transcript or in-memory chat needed by subsequent CLI readers
    return locals()


def test_prompt_to_deliverable_flow(workflow):
    w = workflow
    db, repo, cid = w['db'], w['repo'], w['cid']
    def has(source, relationship, target):
        return any(e['relationship'] == relationship and e['target_ref'] == target
                   for e in edges(db, repo, source))
    assert has(w['task_ref'], 'derived_from', w['prompt']['ref'])
    assert has(w['criterion_ref'], 'derived_from', w['prompt']['ref'])
    receipt = run(db, repo, 'provenance', 'get', w['decision']['receipt_refs'][0])['receipt']
    assert receipt['context']['source_ref'] == w['prompt']['ref']
    assert receipt['context']['session_id'] == w['started']['session_id']
    assert receipt['context']['skill_run_id'] == w['started']['skill_run']['run_id']
    assert any(e['ref'] == w['old_ref'] for e in receipt['effects'])
    assert has(w['new_ref'], 'responds_to', w['finding_ref'])
    assert has(w['new_ref'], 'supersedes', w['old_ref'])
    assert has(w['new_ref'], 'supports', w['criterion_ref'])
    assert has(w['followup_ref'], 'responds_to', w['finding_ref'])
    assert has(w['artifact_ref'], 'implements', w['criterion_ref'])
    assert has(w['proof']['ref'], 'verifies', w['artifact_ref'])
    assert w['proof']['evidence']['result']['automated'] == 1
    assert run(db, repo, 'provenance', 'get', w['artifact_ref'])['artifact']['version'] == git(repo, 'rev-parse', 'HEAD')
    all_proof = run(db, repo, 'provenance', 'evidence')['evidence']
    reviewed = next(p['evidence'] for p in all_proof if p['evidence']['mode'] == 'review')
    assert run(db, repo, 'provenance', 'get', reviewed['artifact_ref'])['artifact']['version'] == w['review']['range']
    run(db, repo, 'task-done', 1, '--reason', 'completed')
    assert run(db, repo, 'task-get', 1)['task']['status'] == 'Done'
    assert run(db, repo, 'provenance', 'get', w['proof']['ref']) == w['proof']


def test_resume_without_transcript(workflow):
    db, repo = workflow['db'], workflow['repo']
    # These reads use only a task ID; source identities are recovered from saved links.
    run(db, repo, 'task-start', 1, '--force', '--force-session')
    brief = run(db, repo, 'task-brief', 1)
    active = run(db, repo, 'context', 'list', 1, '--status', 'active')
    assert {c['content'] for c in active} == {
        'Keep history; read only active decisions.',
        'How long should historical revisions be retained?'}
    task_ref = ref(db, repo, 'task', brief['task']['id'])
    sources = [e['target_ref'] for e in edges(db, repo, task_ref) if e['relationship'] == 'derived_from']
    assert len(sources) == 1
    source = run(db, repo, 'provenance', 'get', sources[0])
    assert source['prompt']['content'] == 'Keep one durable decision and verify the delivered revision.'
    assert source['prompt']['identity_status'] == 'unknown'
    assert 'blue button' not in json.dumps(brief) + json.dumps(source)
    criterion = brief['acceptance_criteria'][0]
    proof = run(db, repo, 'provenance', 'evidence', '--criterion-id', criterion['id'])['evidence'][0]
    assert proof['evidence']['result']['automated'] == 1
    assert run(db, repo, 'provenance', 'get', proof['evidence']['artifact_ref'])['artifact']['version'] == git(repo, 'rev-parse', 'HEAD')
    # Ordinary legacy tasks keep working without any prompt identity.
    legacy = run(db, repo, 'task-insert', 'Legacy standalone task', 'No prompt is available.', '--criteria', 'Legacy completion', '--skip-dupe')
    assert edges(db, repo, ref(db, repo, 'task', legacy['task_id'])) == []
    started = run(db, repo, 'task-start', legacy['task_id'], '--force')
    for receipt_ref in started.get('receipt_refs', []):
        assert 'source_ref' not in run(db, repo, 'provenance', 'get', receipt_ref)['receipt']['context']


def test_supported_skill_commands(project):
    db, repo, _ = project
    names = ('create-task', 'tusk', 'resume-task', 'review-commits', 'retro')
    commands = set()
    for name in names:
        sections = []
        for path in (ROOT / 'skills' / name / 'SKILL.md', ROOT / 'codex-prompts' / (name + '.md')):
            section = path.read_text().split('## Provenance handoff\n', 1)[1].split('\n## ', 1)[0]
            sections.append(section)
            assert 'TUSK_ACTION_SOURCE_REF' in section and 'receipt_refs' in section
            assert 'Missing sources stay unknown' in section
            commands.update(re.findall(r'(?:`(?:tusk )?|^tusk )provenance ([a-z][a-z-]+)', section, re.M))
        assert sections[0] == sections[1], name
    # Ask the real parser about every documented provenance subcommand.
    for command in commands:
        result = subprocess.run([str(CLI), 'provenance', command, '--help'], cwd=repo,
                                env=dict(os.environ, TUSK_DB=str(db), TUSK_PROJECT=str(repo)),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (command, result.stderr)
    assert {'capture-prompt', 'register', 'link', 'links', 'get', 'receipts', 'evidence',
            'capture-artifact', 'declare-evidence'} <= commands
