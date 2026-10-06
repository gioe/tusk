#!/usr/bin/env python3
"""Project-scoped references and causal links; invoked through tusk provenance.

Domain rows stay authoritative. Registered external records carry identity and
an optional locator only, not prompt content, action receipts, or proof.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db = tusk_loader.load("tusk-db-lib")
_json = tusk_loader.load("tusk-json-lib")

# Frozen v89 schema vocabulary. Future migrations must not change this builder
# in place: old migrations need to keep producing the same historical schema.
NATIVE_V89 = {
    "objective": "objectives",
    "task": "tasks",
    "criterion": "acceptance_criteria",
    "context": "task_context_items",
    "review": "code_reviews",
    "finding": "review_comments",
    "session": "task_sessions",
    "skill_run": "skill_runs",
    "progress": "task_progress",
    "jot": "jots",
    "retro": "retro_findings",
}
EXTERNAL_V89 = ("prompt", "action", "evidence", "artifact")
KINDS = (*NATIVE_V89, *EXTERNAL_V89)


def relationship_pairs_v89() -> dict[str, set[tuple[str, str]]]:
    """Direction reads as a sentence: source RELATION target."""
    def pairs(sources, targets):
        return {(s, t) for s in sources for t in targets}

    return {
        "derived_from": pairs(
            ("objective", "task", "criterion", "context", "prompt", "action", "evidence", "artifact", "jot", "retro"),
            ("prompt", "objective", "task", "criterion", "context", "finding", "action", "evidence", "artifact", "progress", "jot", "retro"),
        ),
        "responds_to": pairs(
            ("task", "criterion", "context", "action", "artifact", "review", "finding", "prompt", "retro"),
            ("prompt", "finding", "context", "criterion", "review", "retro"),
        ),
        "supports": pairs(
            ("context", "evidence", "finding", "artifact", "progress", "jot", "retro"),
            ("objective", "task", "criterion", "context"),
        ),
        "implements": pairs(("artifact", "action", "task"), ("objective", "task", "criterion", "context")),
        "verifies": pairs(("evidence", "review"), ("criterion", "artifact")),
        "supersedes": {(kind, kind) for kind in ("context", "prompt", "artifact", "criterion", "review")},
    }


def schema_v89_sql() -> str:
    """DDL shared by fresh init and migration 89, inside caller transaction."""
    kinds = ",".join(repr(k) for k in KINDS)
    native = ",".join(repr(k) for k in NATIVE_V89)
    external = ",".join(repr(k) for k in EXTERNAL_V89)
    sql = f"""
CREATE TABLE provenance_project (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    project_id TEXT NOT NULL UNIQUE
);
INSERT INTO provenance_project VALUES (1, lower(hex(randomblob(16))));
CREATE TABLE provenance_records (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ({kinds})),
    native_id INTEGER,
    external_key TEXT,
    locator TEXT,
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK (availability IN ('available', 'unavailable')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    unavailable_at TEXT,
    CHECK ((kind IN ({native}) AND native_id > 0 AND native_id IS NOT NULL
            AND external_key IS NULL AND locator IS NULL)
        OR (kind IN ({external}) AND native_id IS NULL
            AND external_key IS NOT NULL
            AND length(trim(external_key, ' ' || char(9) || char(10) || char(13))) > 0)),
    CHECK ((availability = 'available' AND unavailable_at IS NULL)
        OR (availability = 'unavailable' AND unavailable_at IS NOT NULL))
);
CREATE UNIQUE INDEX idx_provenance_native ON provenance_records(kind, native_id)
    WHERE availability = 'available' AND native_id IS NOT NULL;
CREATE UNIQUE INDEX idx_provenance_external ON provenance_records(kind, external_key)
    WHERE external_key IS NOT NULL;
CREATE TABLE provenance_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES provenance_records(id) ON DELETE RESTRICT,
    relationship TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES provenance_records(id) ON DELETE RESTRICT,
    attribution TEXT NOT NULL DEFAULT 'explicit' CHECK (attribution IN ('explicit','inferred')),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (source_id <> target_id),
    CHECK (attribution <> 'inferred' OR (reason IS NOT NULL
        AND length(trim(reason, ' ' || char(9) || char(10) || char(13))) > 0)),
    UNIQUE (source_id, relationship, target_id, attribution)
);
CREATE INDEX idx_provenance_links_target ON provenance_links(target_id, id);
CREATE INDEX idx_provenance_links_source ON provenance_links(source_id, id);
CREATE TRIGGER provenance_project_immutable_update BEFORE UPDATE ON provenance_project
BEGIN SELECT RAISE(ABORT, 'provenance project identity is immutable'); END;
CREATE TRIGGER provenance_project_immutable_delete BEFORE DELETE ON provenance_project
BEGIN SELECT RAISE(ABORT, 'provenance project identity is immutable'); END;
CREATE TRIGGER provenance_project_no_replace BEFORE INSERT ON provenance_project
WHEN EXISTS (SELECT 1 FROM provenance_project)
BEGIN SELECT RAISE(ABORT, 'provenance project identity is immutable'); END;
CREATE TRIGGER provenance_record_no_replace BEFORE INSERT ON provenance_records
WHEN EXISTS (SELECT 1 FROM provenance_records WHERE id = NEW.id
    OR (kind = NEW.kind AND external_key = NEW.external_key)
    OR (kind = NEW.kind AND native_id = NEW.native_id AND availability = 'available'))
BEGIN SELECT RAISE(ABORT, 'provenance records cannot be replaced'); END;
CREATE TRIGGER provenance_record_identity BEFORE UPDATE ON provenance_records
WHEN NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind
  OR NEW.native_id IS NOT OLD.native_id OR NEW.external_key IS NOT OLD.external_key
  OR NEW.locator IS NOT OLD.locator OR NEW.created_at IS NOT OLD.created_at
  OR OLD.availability = 'unavailable'
BEGIN SELECT RAISE(ABORT, 'provenance identity and tombstones are immutable'); END;
CREATE TRIGGER provenance_record_retain BEFORE DELETE ON provenance_records
BEGIN SELECT RAISE(ABORT, 'retain provenance records; mark external records unavailable'); END;
CREATE TRIGGER provenance_link_immutable_update BEFORE UPDATE ON provenance_links
BEGIN SELECT RAISE(ABORT, 'provenance links are immutable'); END;
CREATE TRIGGER provenance_link_immutable_delete BEFORE DELETE ON provenance_links
BEGIN SELECT RAISE(ABORT, 'retain provenance links for history'); END;
CREATE TRIGGER provenance_link_no_replace BEFORE INSERT ON provenance_links
WHEN EXISTS (SELECT 1 FROM provenance_links WHERE id = NEW.id
    OR (source_id = NEW.source_id AND relationship = NEW.relationship
        AND target_id = NEW.target_id AND attribution = NEW.attribution))
BEGIN SELECT RAISE(ABORT, 'provenance links cannot be replaced'); END;
-- SQLite REPLACE can evict the old open session through its unique task_id
-- index without firing DELETE triggers when recursive_triggers is off.
CREATE TRIGGER provenance_session_no_replace_open BEFORE INSERT ON task_sessions
WHEN NEW.ended_at IS NULL AND EXISTS (
    SELECT 1 FROM task_sessions s JOIN provenance_records r
      ON r.kind = 'session' AND r.native_id = s.id AND r.availability = 'available'
    WHERE s.task_id = NEW.task_id AND s.ended_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'cannot replace a referenced open session'); END;
CREATE TRIGGER provenance_session_no_update_replace BEFORE UPDATE OF task_id, ended_at ON task_sessions
WHEN NEW.ended_at IS NULL AND EXISTS (
    SELECT 1 FROM task_sessions s JOIN provenance_records r
      ON r.kind = 'session' AND r.native_id = s.id AND r.availability = 'available'
    WHERE s.task_id = NEW.task_id AND s.ended_at IS NULL AND s.id <> OLD.id)
BEGIN SELECT RAISE(ABORT, 'cannot replace a referenced open session'); END;
"""
    for kind, table in NATIVE_V89.items():
        sql += f"""
CREATE TRIGGER provenance_{kind}_exists BEFORE INSERT ON provenance_records
WHEN NEW.kind = '{kind}' AND NOT EXISTS (SELECT 1 FROM {table} WHERE id = NEW.native_id)
BEGIN SELECT RAISE(ABORT, 'provenance native endpoint does not exist'); END;
CREATE TRIGGER provenance_{kind}_deleted AFTER DELETE ON {table}
BEGIN
    UPDATE provenance_records SET availability = 'unavailable', unavailable_at = datetime('now')
    WHERE kind = '{kind}' AND native_id = OLD.id AND availability = 'available';
END;
CREATE TRIGGER provenance_{kind}_identity BEFORE UPDATE OF id ON {table}
WHEN OLD.id IS NOT NEW.id AND EXISTS (
    SELECT 1 FROM provenance_records WHERE kind = '{kind}' AND native_id IN (OLD.id, NEW.id)
    AND availability = 'available')
BEGIN SELECT RAISE(ABORT, 'cannot change the identity of a referenced record'); END;
CREATE TRIGGER provenance_{kind}_no_replace BEFORE INSERT ON {table}
WHEN EXISTS (SELECT 1 FROM {table} WHERE id = NEW.id)
 AND EXISTS (SELECT 1 FROM provenance_records WHERE kind = '{kind}'
    AND native_id = NEW.id AND availability = 'available')
BEGIN SELECT RAISE(ABORT, 'cannot replace a referenced domain record'); END;
"""
    allowed = []
    for relation, pairs in relationship_pairs_v89().items():
        combinations = " OR ".join(
            f"(s.kind = '{s}' AND t.kind = '{t}')" for s, t in sorted(pairs)
        )
        allowed.append(f"(NEW.relationship = '{relation}' AND ({combinations}))")
    sql += f"""
CREATE TRIGGER provenance_link_validate BEFORE INSERT ON provenance_links
WHEN NOT EXISTS (
    SELECT 1 FROM provenance_records s, provenance_records t
    WHERE s.id = NEW.source_id AND t.id = NEW.target_id
      AND s.availability = 'available' AND t.availability = 'available'
      AND ({' OR '.join(allowed)})
)
BEGIN SELECT RAISE(ABORT, 'invalid provenance relationship or unavailable endpoint'); END;
CREATE TRIGGER provenance_supersedes_cycle BEFORE INSERT ON provenance_links
WHEN NEW.relationship = 'supersedes' AND EXISTS (
    WITH RECURSIVE successors(id) AS (
        SELECT NEW.target_id
        UNION
        SELECT l.target_id FROM provenance_links l JOIN successors s ON l.source_id = s.id
        WHERE l.relationship = 'supersedes'
    ) SELECT 1 FROM successors WHERE id = NEW.source_id
)
BEGIN SELECT RAISE(ABORT, 'supersedes would create a cycle'); END;
"""
    return sql


def project_id(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT project_id FROM provenance_project WHERE singleton = 1").fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        raise ValueError("provenance requires schema 89; run tusk migrate") from exc
    if row is None:
        raise ValueError("provenance project identity is missing")
    return row[0]


def schema_v90_sql() -> str:
    """Frozen prompt snapshot DDL; appended to v89 on fresh initialization."""
    return """
CREATE TABLE provenance_prompts (
    record_id TEXT PRIMARY KEY NOT NULL REFERENCES provenance_records(id) ON DELETE RESTRICT,
    provider TEXT,
    conversation_id TEXT,
    message_id TEXT,
    content TEXT NOT NULL CHECK (length(content) > 0),
    representation TEXT NOT NULL CHECK (representation IN ('excerpt', 'summary')),
    truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_provenance_prompt_identity
    ON provenance_prompts(provider, conversation_id, message_id)
    WHERE provider IS NOT NULL AND conversation_id IS NOT NULL AND message_id IS NOT NULL;
CREATE TRIGGER provenance_prompt_endpoint BEFORE INSERT ON provenance_prompts
WHEN NOT EXISTS (SELECT 1 FROM provenance_records
    WHERE id = NEW.record_id AND kind = 'prompt' AND availability = 'available')
BEGIN SELECT RAISE(ABORT, 'prompt snapshot requires an available prompt reference'); END;
CREATE TRIGGER provenance_prompt_immutable_update BEFORE UPDATE ON provenance_prompts
BEGIN SELECT RAISE(ABORT, 'prompt snapshots are immutable'); END;
CREATE TRIGGER provenance_prompt_immutable_delete BEFORE DELETE ON provenance_prompts
BEGIN SELECT RAISE(ABORT, 'retain durable prompt snapshots'); END;
CREATE TRIGGER provenance_prompt_no_replace BEFORE INSERT ON provenance_prompts
WHEN EXISTS (SELECT 1 FROM provenance_prompts WHERE record_id = NEW.record_id
    OR (provider = NEW.provider AND conversation_id = NEW.conversation_id AND message_id = NEW.message_id))
BEGIN SELECT RAISE(ABORT, 'prompt snapshots cannot be replaced'); END;
"""


def prompt_snapshot(conn: sqlite3.Connection, record_id: str, *, required: bool = False) -> dict | None:
    try:
        row = conn.execute('SELECT * FROM provenance_prompts WHERE record_id = ?', (record_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: provenance_prompts' not in str(exc):
            raise
        if required:
            raise ValueError('prompt capture requires schema 90; run tusk migrate') from exc
        return None  # Legacy identity-only registrations still work on schema 89.
    if row is None:
        return None
    snapshot = dict(row)
    snapshot.pop('record_id')
    snapshot['truncated'] = bool(snapshot['truncated'])
    fields = [snapshot[k] for k in ('provider', 'conversation_id', 'message_id')]
    snapshot['identity_status'] = 'complete' if all(fields) else ('partial' if any(fields) else 'unknown')
    return snapshot


def read_prompt_input(args: argparse.Namespace) -> tuple[str, bool]:
    """Read once before transaction/retry; never seek out a transcript."""
    if not 1 <= args.max_chars <= 65536:
        raise ValueError('--max-chars must be between 1 and 65536')
    if args.file is not None:
        with open(args.file, encoding='utf-8', newline='') as stream:
            content = stream.read(args.max_chars + 1)
    else:
        # Preserve CRLF and literal shell syntax just as the file path does.
        sys.stdin.reconfigure(encoding='utf-8', errors='strict', newline='')
        content = sys.stdin.read(args.max_chars + 1)
    truncated = args.truncated or len(content) > args.max_chars
    content = content[:args.max_chars]
    if not content.strip():
        raise ValueError('prompt snapshot must contain non-whitespace text')
    if '\x00' in content:
        raise ValueError('prompt snapshot must be text without NUL characters')
    return content, truncated


def capture_prompt(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    # Check schema before registering anything. The caller owns one transaction
    # covering reference registration plus snapshot persistence.
    prompt_snapshot(conn, '', required=True)
    identity = (args.provider, args.conversation_id, args.message_id)
    for value in (*identity, args.key, args.locator):
        if value is not None and not value.strip():
            raise ValueError('provided identity fields, key, and locator must not be blank')
    key = args.key
    if all(value is not None for value in identity):
        existing = conn.execute(
            'SELECT r.external_key FROM provenance_prompts p JOIN provenance_records r ON r.id = p.record_id '
            'WHERE p.provider = ? AND p.conversation_id = ? AND p.message_id = ?', identity,
        ).fetchone()
        if existing:
            if key is not None and key != existing[0]:
                raise ValueError('message identity already captured with a different key')
            key = existing[0]
        elif key is None:
            key = 'message:' + json.dumps(identity, ensure_ascii=False, separators=(',', ':'))
    if key is None:
        # An opaque capture ID is not a fabricated provider/message identity.
        # Reuse the returned external_key for retries lacking a complete triple.
        key = args.capture_key
    record = register(conn, 'prompt', key=key, locator=args.locator)
    snapshot = prompt_snapshot(conn, record['id'], required=True)
    expected = dict(zip(('provider', 'conversation_id', 'message_id'), identity))
    expected.update(content=args.content, representation=args.representation, truncated=args.input_truncated)
    if snapshot is not None:
        if any(snapshot[k] != v for k, v in expected.items()):
            raise ValueError('prompt already captured with different content or metadata; stored snapshot is immutable')
    else:
        conn.execute(
            'INSERT INTO provenance_prompts(record_id, provider, conversation_id, message_id, content, representation, truncated) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (record['id'], *identity, args.content, args.representation, int(args.input_truncated)),
        )
    return record_output(conn, fetch_record(conn, record['ref']))


def reference(project: str, record_id: str) -> str:
    return f"tusk:{project}:{record_id}"


def record_output(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    result = dict(row)
    result['ref'] = reference(project_id(conn), row['id'])
    if row['kind'] == 'action':
        result['receipt'] = tusk_loader.load('tusk-action-lib').receipt(conn, row['id'])
    if row['kind'] == 'prompt':
        result['prompt'] = prompt_snapshot(conn, row['id'])
    return result


def fetch_record(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    parts = ref.split(':')
    if len(parts) != 3 or parts[0] != 'tusk' or not all(
        re.fullmatch(r'[0-9a-f]{32}', part) for part in parts[1:]
    ):
        raise ValueError("expected a full tusk:<project-id>:<record-id> reference")
    if parts[1] != project_id(conn):
        raise ValueError("reference belongs to a different project")
    row = conn.execute("SELECT * FROM provenance_records WHERE id = ?", (parts[2],)).fetchone()
    if row is None:
        raise ValueError("provenance reference not found")
    return row


def register(conn: sqlite3.Connection, kind: str, native_id: int | None = None,
             *, key: str | None = None, locator: str | None = None) -> dict:
    """Register identity only; caller owns transaction and retry boundaries."""
    project_id(conn)
    if kind not in KINDS:
        raise ValueError("unsupported provenance kind")
    if kind in NATIVE_V89:
        if native_id is None or native_id <= 0 or key is not None or locator is not None:
            raise ValueError("native records require a positive ID and forbid --key/--locator")
        if conn.execute(f"SELECT 1 FROM {NATIVE_V89[kind]} WHERE id = ?", (native_id,)).fetchone() is None:
            raise ValueError(f"{kind} {native_id} not found")
        row = conn.execute(
            "SELECT * FROM provenance_records WHERE kind = ? AND native_id = ? AND availability = 'available'",
            (kind, native_id),
        ).fetchone()
    else:
        if native_id is not None or key is None or not key.strip():
            raise ValueError("external records require a nonempty --key and forbid a native ID")
        if locator is not None and not locator.strip():
            raise ValueError("--locator must not be empty")
        row = conn.execute(
            "SELECT * FROM provenance_records WHERE kind = ? AND external_key = ?", (kind, key)
        ).fetchone()
        if row is not None and locator is not None and locator != row['locator']:
            raise ValueError("existing external identity has a different locator; use a new versioned key")
    if row is None:
        rid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO provenance_records(id, kind, native_id, external_key, locator) VALUES (?, ?, ?, ?, ?)",
            (rid, kind, native_id, key, locator),
        )
        row = conn.execute("SELECT * FROM provenance_records WHERE id = ?", (rid,)).fetchone()
    return record_output(conn, row)


def link(conn: sqlite3.Connection, source: str, relationship: str, target: str,
         *, attribution: str = 'explicit', reason: str | None = None) -> dict:
    """Persist a declared edge, never infer one from chronology or membership."""
    s, t = fetch_record(conn, source), fetch_record(conn, target)
    if attribution == 'inferred' and (reason is None or not reason.strip()):
        raise ValueError("inferred relationships require a nonempty --reason")
    if reason is not None:
        reason = reason.strip()
        if not reason:
            raise ValueError("--reason must not be empty")
    row = conn.execute(
        "SELECT * FROM provenance_links WHERE source_id = ? AND relationship = ? AND target_id = ? AND attribution = ?",
        (s['id'], relationship, t['id'], attribution),
    ).fetchone()
    if row is not None:
        if reason is not None and reason != row['reason']:
            raise ValueError("existing relationship has a different reason; history is immutable")
    else:
        cur = conn.execute(
            "INSERT INTO provenance_links(source_id, relationship, target_id, attribution, reason) VALUES (?, ?, ?, ?, ?)",
            (s['id'], relationship, t['id'], attribution, reason),
        )
        row = conn.execute("SELECT * FROM provenance_links WHERE id = ?", (cur.lastrowid,)).fetchone()
    return link_output(conn, row)


def link_output(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    out = dict(row)
    project = project_id(conn)
    for field in ('source_id', 'target_id'):
        out[field.replace('_id', '_ref')] = reference(project, out.pop(field))
    return out


def dispatch(conn: sqlite3.Connection, args: argparse.Namespace):
    if args.command == 'receipts':
        return tusk_loader.load('tusk-action-lib').list_receipts(conn, args.task_id, args.limit)
    if args.command == 'capture-prompt':
        return capture_prompt(conn, args)
    if args.command == 'register':
        return register(conn, args.kind, args.native_id, key=args.key, locator=args.locator)
    if args.command == 'get':
        return record_output(conn, fetch_record(conn, args.ref))
    if args.command == 'link':
        return link(conn, args.source, args.relationship, args.target, attribution=args.attribution, reason=args.reason)
    row = fetch_record(conn, args.ref)
    if args.command == 'unavailable':
        if row['kind'] not in EXTERNAL_V89:
            raise ValueError("native availability follows deletion of its domain record")
        if row['availability'] == 'available':
            conn.execute(
                "UPDATE provenance_records SET availability = 'unavailable', unavailable_at = datetime('now') WHERE id = ?",
                (row['id'],),
            )
        return record_output(conn, fetch_record(conn, args.ref))
    if args.limit < 1 or args.limit > 1000:
        raise ValueError("--limit must be between 1 and 1000")
    where, values = {
        'outgoing': ('source_id = ?', (row['id'],)),
        'incoming': ('target_id = ?', (row['id'],)),
        'both': ('source_id = ? OR target_id = ?', (row['id'], row['id'])),
    }[args.direction]
    rows = conn.execute(
        f"SELECT * FROM provenance_links WHERE {where} ORDER BY id LIMIT ?",
        (*values, args.limit + 1),
    ).fetchall()
    return {'links': [link_output(conn, r) for r in rows[:args.limit]], 'truncated': len(rows) > args.limit}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='tusk provenance', allow_abbrev=False,
        description='Register durable project-scoped identities and explicit or inferred causal links. '
                    'Existing domain rows stay authoritative; external registration is not proof. '
                    'Covered mutations return receipt_refs; get REF shows transaction effects and '
                    'execution context. mutation_committed is not verification or command exit status. '
                    'Explicit context: TUSK_ACTION_TASK_ID, TUSK_ACTION_SESSION_ID, '
                    'TUSK_ACTION_SKILL_RUN_ID, TUSK_ACTION_WORKSPACE_ID, TUSK_ACTION_SOURCE_REF. ')
    sub = parser.add_subparsers(dest='command', required=True)
    receipts = sub.add_parser('receipts', allow_abbrev=False,
        help='List committed transaction receipts, including partial-command work; not verification proof.')
    receipts.add_argument('--task-id', type=int, help='Filter by affected task (including deleted records).')
    receipts.add_argument('--limit', type=int, default=20, help='Newest first, 1..1000, default 20.')
    capture = sub.add_parser('capture-prompt', allow_abbrev=False,
        help='Save an explicit UTF-8 excerpt or summary before a task exists; never discovers transcripts.')
    source = capture.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', help='File containing only the selected excerpt/summary to save.')
    source.add_argument('--stdin', action='store_true', help='Read selected text from standard input.')
    capture.add_argument('--representation', choices=('excerpt', 'summary'), required=True,
                         help='Label supplied text; the CLI does not summarize it.')
    capture.add_argument('--provider', help='Explicit provider name; omitted means unknown.')
    capture.add_argument('--conversation-id')
    capture.add_argument('--message-id')
    capture.add_argument('--key', help='Stable retry key; defaults to full message identity or a generated capture key.')
    capture.add_argument('--locator', help='Optional source locator; never read or required for retrieval.')
    capture.add_argument('--max-chars', type=int, default=8192, help='Stored character limit, 1..65536 (default 8192).')
    capture.add_argument('--truncated', action='store_true', help='Declare supplied text was already truncated before capture.')
    reg = sub.add_parser('register', allow_abbrev=False, help='Register a native row or an external identity; idempotent.')
    reg.add_argument('kind', choices=KINDS)
    reg.add_argument('native_id', type=int, nargs='?')
    reg.add_argument('--key', help='Opaque namespaced/versioned identity for prompt/action/evidence/artifact.')
    reg.add_argument('--locator', help='Optional external locator; never fetched.')
    for name, help_text in (('get', 'Inspect one reference, including tombstones.'),
                            ('unavailable', 'Retain an external record as an irreversible tombstone.')):
        cmd = sub.add_parser(name, allow_abbrev=False, help=help_text)
        cmd.add_argument('ref')
    edge = sub.add_parser('link', allow_abbrev=False, help='Assert source RELATION target; supersedes points from replacement to old record.')
    edge.add_argument('source')
    edge.add_argument('relationship', choices=relationship_pairs_v89())
    edge.add_argument('target')
    edge.add_argument('--attribution', choices=('explicit', 'inferred'), default='explicit')
    edge.add_argument('--reason', help='Required for inferred links; does not assert verification success.')
    ls = sub.add_parser('links', allow_abbrev=False, help='Inspect adjacent links; no recursive traversal.')
    ls.add_argument('ref')
    ls.add_argument('--direction', choices=('incoming', 'outgoing', 'both'), default='both')
    ls.add_argument('--limit', type=int, default=100)
    args = parser.parse_args(argv[2:])
    if args.command == 'capture-prompt':
        try:
            args.content, args.input_truncated = read_prompt_input(args)
        except (ValueError, OSError, UnicodeError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
        args.capture_key = 'capture:' + uuid.uuid4().hex

    def run():
        conn = _db.get_connection(argv[0])
        try:
            # Acquire writer lock before validating endpoints, closing the
            # registration-vs-deletion and duplicate-registration race windows.
            if args.command not in ('get', 'links', 'receipts'):
                conn.execute('BEGIN IMMEDIATE')
            result = dispatch(conn, args)
            conn.commit()
            return result
        finally:
            conn.close()

    try:
        print(_json.dumps(_db.retry_on_locked(run, label='provenance')))
        return 0
    except (ValueError, sqlite3.Error) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if len(sys.argv) < 3 or not sys.argv[1].endswith('.db'):
        sys.exit('Error: invoke through tusk provenance --help')
    sys.exit(main(sys.argv[1:]))
