"""Immutable evidence attempts and artifact identities (tusk-evidence-lib.py).

A start is durable before executing a command; a separate, append-only result
finalizes it. A missing result means interrupted/unknown, never success.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

import tusk_loader

_db = tusk_loader.load('tusk-db-lib')


def provenance():
    return tusk_loader.load('tusk-provenance')


def enabled(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'provenance_attempts'").fetchone())


def schema_v92_sql():
    sql = """
CREATE TABLE provenance_artifacts (
 record_id TEXT PRIMARY KEY NOT NULL REFERENCES provenance_records(id),
 artifact_type TEXT NOT NULL,
 uri TEXT NOT NULL,
 version TEXT,
 digest TEXT,
 attribution TEXT NOT NULL CHECK(attribution IN ('observed','declared'))
);
CREATE TABLE provenance_attempts (
 record_id TEXT PRIMARY KEY NOT NULL REFERENCES provenance_records(id),
 task_id INTEGER,
 criterion_id TEXT REFERENCES provenance_records(id),
 review_id TEXT REFERENCES provenance_records(id),
 action_id TEXT NOT NULL REFERENCES provenance_actions(record_id),
 artifact_id TEXT REFERENCES provenance_artifacts(record_id),
 source_id TEXT REFERENCES provenance_records(id),
 mode TEXT NOT NULL CHECK(mode IN ('executed','reused','bypassed','external','manual','review')),
 spec TEXT,
 details_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_provenance_attempt_task ON provenance_attempts(task_id);
CREATE INDEX idx_provenance_attempt_criterion ON provenance_attempts(criterion_id);
CREATE TABLE provenance_results (
 attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES provenance_attempts(record_id),
 action_id TEXT NOT NULL REFERENCES provenance_actions(record_id),
 outcome TEXT NOT NULL CHECK(outcome IN ('passed','failed','bypassed','declared_passed','declared_failed','approved','changes_requested','unknown')),
 automated INTEGER NOT NULL CHECK(automated IN (0,1)),
 target_matches INTEGER NOT NULL CHECK(target_matches IN (0,1)),
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 CHECK(automated = 0 OR (outcome = 'passed' AND target_matches = 1))
);
CREATE TABLE provenance_review_targets (
 review_id TEXT PRIMARY KEY NOT NULL REFERENCES provenance_records(id),
 artifact_id TEXT REFERENCES provenance_artifacts(record_id),
 details_json TEXT NOT NULL
);
"""
    for table, key in (('provenance_artifacts','record_id'), ('provenance_attempts','record_id'),
                       ('provenance_results','attempt_id'), ('provenance_review_targets','review_id')):
        for op in ('UPDATE','DELETE'):
            sql += f"CREATE TRIGGER {table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'evidence is immutable'); END;\n"
        sql += f"CREATE TRIGGER {table}_replace BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM {table} WHERE {key}=NEW.{key}) BEGIN SELECT RAISE(ABORT,'evidence is immutable'); END;\n"
    for table, key, kind in (('provenance_artifacts','record_id','artifact'), ('provenance_attempts','record_id','evidence'),
                             ('provenance_review_targets','review_id','review')):
        sql += f"CREATE TRIGGER {table}_kind BEFORE INSERT ON {table} WHEN NOT EXISTS (SELECT 1 FROM provenance_records WHERE id=NEW.{key} AND kind='{kind}') BEGIN SELECT RAISE(ABORT,'invalid evidence record kind'); END;\n"
    return sql


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError('git target unavailable')
    return result.stdout


def snapshot(root=None):
    """Fingerprint Git-visible files, including dirty/untracked source content.

    This is an observation of files, not a filesystem-atomic snapshot or a
    claim about ignored dependencies/runtime state. No file contents are saved.
    """
    try:
        root = git(root or os.getcwd(), 'rev-parse', '--show-toplevel').decode().strip()
        head = git(root, 'rev-parse', 'HEAD').decode().strip()
        paths = set(git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''}
        digest = hashlib.sha256()
        for name in sorted(paths):
            path = Path(root) / os.fsdecode(name)
            digest.update(len(name).to_bytes(8, 'big') + name)
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                digest.update(b'missing')
                continue
            if stat.S_ISLNK(mode):
                digest.update(b'link' + os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                digest.update(b'executable' if mode & 0o111 else b'file')
                content = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        content.update(block)
                digest.update(content.digest())
            else:
                return {'state': 'unknown', 'reason': 'non-file Git entry'}
        clean = not git(root, 'status', '--porcelain', '--untracked-files=normal').strip()
        return {'state': 'clean' if clean else 'dirty', 'head': head,
                'digest': 'sha256:' + digest.hexdigest(), 'root': root}
    except (OSError, ValueError, UnicodeError):
        return {'state': 'unknown', 'reason': 'Git or source content unavailable'}


def artifact(conn, artifact_type, uri, version=None, digest=None, attribution='declared', unavailable=False):
    if not uri or not uri.strip() or any(value is not None and not value.strip() for value in (version, digest)):
        raise ValueError('artifact URI must be nonempty; supplied version/digest must be nonempty')
    identity = json.dumps([artifact_type, uri, version, digest, attribution], separators=(',', ':'))
    ref = provenance().register(conn, 'artifact', key='artifact:' + hashlib.sha256(identity.encode()).hexdigest())
    if conn.execute('SELECT 1 FROM provenance_artifacts WHERE record_id=?', (ref['id'],)).fetchone() is None:
        conn.execute('INSERT INTO provenance_artifacts VALUES (?,?,?,?,?,?)',
                     (ref['id'], artifact_type, uri, version, digest, attribution))
    if unavailable and ref['availability'] == 'available':
        conn.execute("UPDATE provenance_records SET availability='unavailable',unavailable_at=datetime('now') WHERE id=?", (ref['id'],))
    return ref['ref']


def snapshot_artifact(conn, target):
    if not target.get('digest'):
        return None
    return artifact(conn, 'commit' if target['state'] == 'clean' else 'working_tree',
                    'git-worktree:' + target['root'],
                    target['head'] if target['state'] == 'clean' else None,
                    target['digest'], 'observed')


def _id(conn, ref):
    return provenance().fetch_record(conn, ref)['id'] if ref else None


def _receipt(conn, action, ref, task_id, operation):
    conn.execute('INSERT OR IGNORE INTO action_effects VALUES (?,?,?)', (_id(conn, ref), operation, task_id))
    return _id(conn, action.flush(conn))


def start(conn, action, *, task_id=None, criterion_id=None, review_id=None, mode='executed',
          spec=None, target=None, artifact_ref=None, source_ref=None, details=None, commit=True):
    if not enabled(conn):
        return None
    p = provenance()
    ref = p.register(conn, 'evidence', key='attempt:' + uuid.uuid4().hex)['ref']
    criterion_ref = p.register(conn, 'criterion', criterion_id)['ref'] if criterion_id else None
    review_ref = p.register(conn, 'review', review_id)['ref'] if review_id else None
    target = target if target is not None else {'state': 'unknown'}
    artifact_ref = artifact_ref or snapshot_artifact(conn, target)
    action_id = _receipt(conn, action, ref, task_id, 'insert')
    metadata = dict(details or {}, target=target)
    if criterion_id:
        criterion = conn.execute('SELECT criterion,criterion_type,verification_spec FROM acceptance_criteria WHERE id=?', (criterion_id,)).fetchone()
        metadata['criterion_snapshot'] = dict(criterion)
    conn.execute('INSERT INTO provenance_attempts(record_id,task_id,criterion_id,review_id,action_id,artifact_id,source_id,mode,spec,details_json) VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (_id(conn, ref), task_id, _id(conn, criterion_ref), _id(conn, review_ref), action_id,
                  _id(conn, artifact_ref), _id(conn, source_ref), mode, spec, json.dumps(metadata)))
    for source in (criterion_ref, artifact_ref, source_ref, p.reference(p.project_id(conn), action_id)):
        if source and p.fetch_record(conn, source)['availability'] == 'available':
            p.link(conn, ref, 'derived_from', source)
    if commit:
        conn.commit()
    return ref


def finish(conn, action, ref, payload, after=None):
    if ref is None:
        return
    p = provenance()
    row = conn.execute('SELECT * FROM provenance_attempts WHERE record_id=?', (_id(conn, ref),)).fetchone()
    if conn.execute('SELECT 1 FROM provenance_results WHERE attempt_id=?', (row['record_id'],)).fetchone():
        return
    before = json.loads(row['details_json'])['target']
    matches = bool(after and before.get('digest') and before.get('digest') == after.get('digest')
                   and before.get('head') == after.get('head'))
    mode = row['mode']
    outcome = 'passed' if payload.get('passed') else 'failed'
    automated = mode == 'executed' and outcome == 'passed' and matches
    if mode in ('external','manual'):
        outcome = 'declared_passed' if payload.get('passed') else 'declared_failed'
    elif mode == 'bypassed':
        outcome = 'bypassed'
    elif mode == 'review':
        outcome = payload['verdict']
        matches = bool(row['artifact_id'])
    elif mode == 'reused':
        source = conn.execute('SELECT a.details_json,a.spec,r.automated,r.outcome FROM provenance_attempts a JOIN provenance_results r ON r.attempt_id=a.record_id WHERE a.record_id=?', (row['source_id'],)).fetchone()
        if source:
            original = json.loads(source['details_json'])['target']
            automated = bool(source['automated'] and source['outcome'] == 'passed' and matches
                             and original.get('digest') == before.get('digest')
                             and row['spec'] == source['spec'])
    if payload.get('unknown'):
        outcome, automated = 'unknown', False
    payload = dict(payload)
    if isinstance(payload.get('output'), str) and len(payload['output']) > 8192:
        payload['output'] = payload['output'][:8192]
        payload['output_truncated'] = True
    action_id = _receipt(conn, action, ref, row['task_id'], 'update')
    conn.execute('INSERT INTO provenance_results(attempt_id,action_id,outcome,automated,target_matches,payload_json) VALUES (?,?,?,?,?,?)',
                 (row['record_id'], action_id, outcome, int(automated), int(matches), json.dumps(payload)))
    p.link(conn, ref, 'derived_from', p.reference(p.project_id(conn), action_id))
    if automated:
        for rid in (row['criterion_id'], row['artifact_id']):
            if rid:
                p.link(conn, ref, 'verifies', p.reference(p.project_id(conn), rid))


def observe(conn, action, *, task_id, criterion_id=None, spec, call, root=None):
    if not enabled(conn):
        return call(), None
    cache = getattr(action, 'observations', None)
    if cache is None:
        action.observations = cache = {}
    key = (task_id, criterion_id, spec)
    if key not in cache:
        target = snapshot(root)
        ref = start(conn, action, task_id=task_id, criterion_id=criterion_id, spec=spec, target=target)
        cache[key] = {'ref': ref, 'target': target}
    entry = cache[key]
    if 'payload' not in entry:
        entry['payload'] = call()
        entry['after'] = snapshot(root)
    return dict(entry['payload']), entry


def output(conn, row):
    if not enabled(conn):
        return None
    p = provenance()
    if row['kind'] == 'artifact':
        found = conn.execute('SELECT * FROM provenance_artifacts WHERE record_id=?', (row['id'],)).fetchone()
        if found is None:
            return None
        result = dict(found)
        result['version_known'] = bool(result['version'] or result['digest'])
        return result
    found = conn.execute('SELECT * FROM provenance_attempts WHERE record_id=?', (row['id'],)).fetchone()
    if found is None:
        return None
    result = dict(found)
    result['details'] = json.loads(result.pop('details_json'))
    for field in ('criterion_id','review_id','action_id','artifact_id','source_id'):
        rid = result.pop(field)
        result[field[:-3] + '_ref'] = p.reference(p.project_id(conn), rid) if rid else None
    final = conn.execute('SELECT * FROM provenance_results WHERE attempt_id=?', (row['id'],)).fetchone()
    result['result'] = None
    result['state'] = 'pending' if final is None else 'finished'
    if final:
        final = dict(final)
        final['payload'] = json.loads(final.pop('payload_json'))
        final['action_ref'] = p.reference(p.project_id(conn), final.pop('action_id'))
        result['result'] = final
    return result


def freeze_review(conn, review_id, root=None, diff_range=None):
    if not enabled(conn):
        return diff_range
    target, ref = {'state': 'unknown'}, None
    if root and diff_range:
        try:
            if '...' in diff_range:
                left, right = diff_range.split('...', 1)
                base = git(root, 'merge-base', left, right).decode().strip()
            else:
                left, right = diff_range.split('..', 1)
                base = git(root, 'rev-parse', left + '^{commit}').decode().strip()
            head = git(root, 'rev-parse', right + '^{commit}').decode().strip()
            diff_range = base + '..' + head
            digest = 'sha256:' + hashlib.sha256(git(root, 'diff', '--binary', diff_range)).hexdigest()
            target = {'state': 'review_range', 'base': base, 'head': head, 'digest': digest}
            ref = artifact(conn, 'review_diff', 'git-review:' + str(root), diff_range, digest, 'observed')
        except (OSError, ValueError):
            pass
    p = provenance()
    review_ref = p.register(conn, 'review', review_id)['ref']
    conn.execute('INSERT INTO provenance_review_targets VALUES (?,?,?)',
                 (_id(conn, review_ref), _id(conn, ref), json.dumps(target)))
    return diff_range


def review_verdict(conn, action, review_id, task_id, verdict, note=None):
    if not enabled(conn):
        return
    p = provenance()
    review_ref = p.register(conn, 'review', review_id)['ref']
    target = conn.execute('SELECT * FROM provenance_review_targets WHERE review_id=?', (_id(conn, review_ref),)).fetchone()
    artifact_ref = p.reference(p.project_id(conn), target['artifact_id']) if target and target['artifact_id'] else None
    review = conn.execute('SELECT reviewer,model,review_pass,diff_range FROM code_reviews WHERE id=?', (review_id,)).fetchone()
    ref = start(conn, action, task_id=task_id, review_id=review_id, mode='review', details=dict(review),
                target=json.loads(target['details_json']) if target else None, artifact_ref=artifact_ref, commit=False)
    finish(conn, action, ref, {'verdict': verdict, 'note': note})
    return ref


def begin_gate(db_path, task_id, root, spec, action=None):
    if not os.path.isfile(db_path):
        return None
    action = action or tusk_loader.load('tusk-action-lib').CLIAction('commit test gate')
    conn = action.get_connection(db_path)
    try:
        if not enabled(conn):
            return None
        target = snapshot(root)
        ref = start(conn, action, task_id=task_id, spec=spec, target=target)
        return {'db_path': db_path, 'action': action, 'ref': ref, 'root': root, 'task_id': task_id, 'spec': spec}
    finally:
        conn.close()


def finish_gate(handle, result, passed=False):
    if handle is None:
        return
    action = handle['action']
    conn = action.get_connection(handle['db_path'])
    try:
        payload = {'passed': passed, 'unknown': result is None,
                   'output': ((result.stdout or '') + '\n' + (result.stderr or ''))[-8192:] if result else 'gate timed out',
                   'exit_code': result.returncode if result else None}
        finish(conn, action, handle['ref'], payload, snapshot(handle['root']))
        conn.commit()
    finally:
        conn.close()
