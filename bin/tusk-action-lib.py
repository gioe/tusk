"""Transactional receipts for explicitly opted-in CLI mutation connections.

TEMP triggers collect affected identities inside the caller's transaction,
including identities about to be deleted. commit() flushes that transaction's
queue before committing. Rollbacks (including savepoint rollbacks) undo both
the queue and identity registration. Other commands/connections are untouched.
"""

import json
import os
import sqlite3
import sys
import uuid

import tusk_loader

_db = tusk_loader.load("tusk-db-lib")
_json = tusk_loader.load("tusk-json-lib")


def schema_v91_sql():
    """Frozen DDL; shared by fresh init and migration 91."""
    sql = """
CREATE TABLE provenance_actions (
    record_id TEXT PRIMARY KEY NOT NULL REFERENCES provenance_records(id),
    invocation_id TEXT NOT NULL,
    command TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome = 'mutation_committed'),
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_provenance_action_invocation ON provenance_actions(invocation_id);
CREATE TABLE provenance_action_effects (
    action_id TEXT NOT NULL REFERENCES provenance_actions(record_id),
    record_id TEXT NOT NULL REFERENCES provenance_records(id),
    operation TEXT NOT NULL CHECK (operation IN ('insert','update','delete')),
    task_id INTEGER,
    PRIMARY KEY (action_id, record_id, operation)
);
CREATE INDEX idx_provenance_action_effect_record ON provenance_action_effects(record_id);
CREATE TRIGGER provenance_action_kind BEFORE INSERT ON provenance_actions
WHEN NOT EXISTS (SELECT 1 FROM provenance_records WHERE id = NEW.record_id AND kind = 'action')
BEGIN SELECT RAISE(ABORT, 'receipt requires an action reference'); END;
CREATE TRIGGER provenance_action_replace BEFORE INSERT ON provenance_actions
WHEN EXISTS (SELECT 1 FROM provenance_actions WHERE record_id = NEW.record_id)
BEGIN SELECT RAISE(ABORT, 'action receipts are immutable'); END;
CREATE TRIGGER provenance_action_effect_replace BEFORE INSERT ON provenance_action_effects
WHEN EXISTS (SELECT 1 FROM provenance_action_effects WHERE action_id = NEW.action_id
  AND record_id = NEW.record_id AND operation = NEW.operation)
BEGIN SELECT RAISE(ABORT, 'action receipts are immutable'); END;
"""
    for table in ('provenance_actions', 'provenance_action_effects'):
        for operation in ('UPDATE', 'DELETE'):
            sql += f"""
CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} ON {table}
BEGIN SELECT RAISE(ABORT, 'action receipts are immutable'); END;
"""
    return sql


def receipt(conn, record_id):
    """Read a receipt; historical identity-only action registrations return null."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'provenance_actions'").fetchone():
        return None
    row = conn.execute('SELECT * FROM provenance_actions WHERE record_id = ?', (record_id,)).fetchone()
    if row is None:
        return None
    provenance = tusk_loader.load('tusk-provenance')
    project = provenance.project_id(conn)
    result = dict(row)
    result['context'] = json.loads(result.pop('context_json'))
    result['is_verification'] = False
    result['effects'] = [dict(ref=provenance.reference(project, r['record_id']),
                              kind=r['kind'], native_id=r['native_id'], operation=r['operation'], task_id=r['task_id'])
                         for r in conn.execute(
                             'SELECT e.record_id, e.operation, e.task_id, r.kind, r.native_id '
                             'FROM provenance_action_effects e JOIN provenance_records r ON r.id = e.record_id '
                             'WHERE e.action_id = ? ORDER BY r.kind, r.native_id, e.operation', (record_id,))]
    return result


def list_receipts(conn, task_id=None, limit=20):
    if not 1 <= limit <= 1000:
        raise ValueError('--limit must be between 1 and 1000')
    provenance = tusk_loader.load('tusk-provenance')
    rows = conn.execute(
        'SELECT a.* FROM provenance_actions a WHERE (? IS NULL OR EXISTS '
        '(SELECT 1 FROM provenance_action_effects e WHERE e.action_id = a.record_id AND e.task_id = ?)) '
        'ORDER BY a.rowid DESC LIMIT ?', (task_id, task_id, limit + 1)).fetchall()
    project = provenance.project_id(conn)
    return {'receipts': [dict(ref=provenance.reference(project, r['record_id']),
                              invocation_id=r['invocation_id'], command=r['command'],
                              outcome=r['outcome'], created_at=r['created_at']) for r in rows[:limit]],
            'truncated': len(rows) > limit}


def _inside_workspace(cwd, workspace):
    # samefile handles case-insensitive filesystems; realpath alone does not
    # canonicalize case on macOS. Never trust a stale, missing workspace path.
    candidate = cwd
    while True:
        try:
            if os.path.samefile(candidate, workspace):
                return True
        except OSError:
            return False
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return False
        candidate = parent


class ActionConnection(sqlite3.Connection):
    def commit(self):
        ref = self.action.flush(self) if getattr(self, 'action', None) else None
        super().commit()
        # Publish only after the actual COMMIT succeeds. Failed COMMIT leaves
        # queue/receipt in the same transaction for rollback or commit retry.
        self.pending_ref = None
        if ref and ref not in self.action.refs:
            self.action.refs.append(ref)


    def rollback(self):
        super().rollback()
        self.pending_ref = None


class CLIAction:
    def __init__(self, command):
        self.command = command
        self.invocation_id = uuid.uuid4().hex
        self.refs = []

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict) and self.refs:
            obj = dict(obj, receipt_refs=list(self.refs))
            self.refs.clear()
        return _json.dumps(obj, **kwargs)

    def get_connection(self, db_path):
        conn = _db.get_connection(db_path, factory=ActionConnection)
        try:
            # Old installations and narrow unit fixtures remain usable during
            # upgrades; installing the schema enables receipts automatically.
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'provenance_actions'").fetchone():
                return conn
            conn.action = self
            conn.explicit_context = self.explicit_context(conn)
            conn.execute('CREATE TEMP TABLE action_effects(record_id TEXT, operation TEXT, task_id INTEGER, '
                         'PRIMARY KEY(record_id, operation))')
            native = {'task': 'tasks', 'criterion': 'acceptance_criteria',
                      'context': 'task_context_items', 'progress': 'task_progress',
                      'session': 'task_sessions', 'skill_run': 'skill_runs'}
            for kind, table in native.items():
                for operation in ('insert', 'update', 'delete'):
                    row = 'OLD' if operation == 'delete' else 'NEW'
                    timing = 'BEFORE' if operation == 'delete' else 'AFTER'
                    task_id = f'{row}.id' if kind == 'task' else f'{row}.task_id'
                    conn.execute(f"""
                        CREATE TEMP TRIGGER action_{kind}_{operation} {timing} {operation} ON main.{table}
                        BEGIN
                          INSERT INTO provenance_records(id,kind,native_id)
                          SELECT lower(hex(randomblob(16))), '{kind}', {row}.id
                          WHERE NOT EXISTS (SELECT 1 FROM provenance_records WHERE kind = '{kind}'
                            AND native_id = {row}.id AND availability = 'available');
                          INSERT OR IGNORE INTO action_effects
                          SELECT id, '{operation}', {task_id} FROM provenance_records
                          WHERE kind = '{kind}' AND native_id = {row}.id AND availability = 'available';
                        END
                    """)
            # Import may only add relationships to reused tasks. Record those
            # task endpoints as updates without inventing a native edge ID.
            for table, columns in (('task_dependencies', ('task_id', 'depends_on_id')),
                                   ('objective_tasks', ('task_id',))):
                for operation in ('insert', 'update', 'delete'):
                    row = 'OLD' if operation == 'delete' else 'NEW'
                    statements = ''
                    for column in columns:
                        statements += f"""
                          INSERT INTO provenance_records(id,kind,native_id)
                          SELECT lower(hex(randomblob(16))), 'task', {row}.{column}
                          WHERE NOT EXISTS (SELECT 1 FROM provenance_records WHERE kind = 'task'
                            AND native_id = {row}.{column} AND availability = 'available');
                          INSERT OR IGNORE INTO action_effects
                          SELECT id, 'update', {row}.{column} FROM provenance_records
                          WHERE kind = 'task' AND native_id = {row}.{column} AND availability = 'available';
                        """
                    conn.execute(f'CREATE TEMP TRIGGER action_{table}_{operation} AFTER {operation} '
                                 f'ON main.{table} BEGIN {statements} END')
            return conn
        except ValueError as exc:
            conn.close()
            print(f'Error: action context: {exc}', file=sys.stderr)
            raise SystemExit(2)
        except BaseException:
            conn.close()
            raise

    def explicit_context(self, conn):
        context = {}
        for field, table in (('task_id', 'tasks'), ('session_id', 'task_sessions'),
                             ('skill_run_id', 'skill_runs'), ('workspace_id', 'task_workspaces')):
            value = os.environ.get('TUSK_ACTION_' + field.upper())
            if value is None:
                continue
            if not value.isdecimal() or int(value) < 1:
                raise ValueError(f'{field} must be a positive integer')
            row = conn.execute(f'SELECT * FROM {table} WHERE id = ?', (int(value),)).fetchone()
            if row is None:
                raise ValueError(f'{field} {value} not found')
            context[field] = int(value)
            if field != 'task_id' and row['task_id'] is not None:
                owner = context.setdefault('task_id', row['task_id'])
                if owner != row['task_id']:
                    raise ValueError('explicit execution identities belong to different tasks')
        source = os.environ.get('TUSK_ACTION_SOURCE_REF')
        if source is not None:
            tusk_loader.load('tusk-provenance').fetch_record(conn, source)
            context['source_ref'] = source
        return context

    def context(self, conn, effects):
        context = dict(conn.explicit_context)
        origins = {key: 'explicit' for key in context}
        ambiguous = []
        # Only the actual caller directory identifies a workspace; never use
        # the latest global session or a neighbouring worktree's environment.
        cwd = os.path.realpath(os.getcwd())
        matches = []
        for row in conn.execute('SELECT id, task_id, workspace_path FROM task_workspaces'):
            path = os.path.realpath(row['workspace_path'])
            if _inside_workspace(cwd, path):
                matches.append(row)
        if 'task_id' not in context:
            candidates = {r['task_id'] for r in matches}
            basis = 'caller_workspace'
            if not candidates:
                candidates = {r['task_id'] for r in effects if r['task_id'] is not None}
                basis = 'affected_task'
            if len(candidates) == 1:
                context['task_id'] = candidates.pop()
                origins['task_id'] = basis
            elif candidates:
                ambiguous.append('task_id')
        if 'workspace_id' not in context:
            matches = [r for r in matches if r['task_id'] == context.get('task_id')]
            if len(matches) == 1:
                context['workspace_id'] = matches[0]['id']
                origins['workspace_id'] = 'caller_workspace'
            elif matches:
                ambiguous.append('workspace_id')
        for field, table, ended in (('session_id', 'task_sessions', 'ended_at'),
                                    ('skill_run_id', 'skill_runs', 'ended_at')):
            if field in context or 'task_id' not in context:
                continue
            rows = conn.execute(f'SELECT id FROM {table} WHERE task_id = ? AND {ended} IS NULL',
                                (context['task_id'],)).fetchall()
            # A session closed by task-done is still available execution
            # context. Do not infer it from timestamps after closure.
            if not rows:
                kind = 'session' if field == 'session_id' else 'skill_run'
                rows = conn.execute(
                    'SELECT DISTINCT r.native_id AS id FROM action_effects e '
                    'JOIN provenance_records r ON r.id = e.record_id '
                    'WHERE r.kind = ? AND e.task_id = ?', (kind, context['task_id'])).fetchall()
                basis = 'affected_execution_record'
            else:
                basis = 'unique_open_for_task'
            if len(rows) == 1:
                context[field] = rows[0]['id']
                origins[field] = basis
            elif rows:
                ambiguous.append(field)
        if 'workspace_id' in context:
            context['workspace_path'] = conn.execute('SELECT workspace_path FROM task_workspaces WHERE id = ?',
                                                     (context['workspace_id'],)).fetchone()[0]
        provenance = tusk_loader.load('tusk-provenance')
        for field, kind in (('task_id', 'task'), ('session_id', 'session'), ('skill_run_id', 'skill_run')):
            if field in context:
                context[field.removesuffix('_id') + '_ref'] = provenance.register(conn, kind, context[field])['ref']
        context['attribution'] = origins
        context['ambiguous'] = ambiguous
        return context

    def flush(self, conn):
        # A commit retry after SQLITE_BUSY reuses the pending receipt rather
        # than inserting a second one. rollback() clears the pending reference.
        pending = getattr(conn, 'pending_ref', None)
        if pending and not conn.execute('SELECT 1 FROM provenance_actions WHERE record_id = ?',
                                        (pending.rsplit(':', 1)[1],)).fetchone():
            pending = conn.pending_ref = None
        effects = conn.execute('SELECT * FROM action_effects').fetchall()
        if not effects:
            return pending
        provenance = tusk_loader.load('tusk-provenance')
        action = provenance.register(conn, 'action', key='receipt:' + uuid.uuid4().hex)
        conn.execute('INSERT INTO provenance_actions(record_id,invocation_id,command,outcome,context_json) '
                     "VALUES (?,?,?,'mutation_committed',?)",
                     (action['id'], self.invocation_id, self.command,
                      json.dumps(self.context(conn, effects), separators=(',', ':'))))
        conn.executemany('INSERT INTO provenance_action_effects VALUES (?,?,?,?)',
                         [(action['id'], r['record_id'], r['operation'], r['task_id']) for r in effects])
        conn.execute('DELETE FROM action_effects')
        conn.pending_ref = action['ref']
        return action['ref']
