"""Observation dispositions: append-only conclusions over immutable capture identity."""
import tusk_loader


def schema_v94_sql():
    return """
CREATE TABLE observation_dispositions (
    context_id INTEGER PRIMARY KEY REFERENCES task_context_items(id) ON DELETE RESTRICT,
    disposition TEXT NOT NULL CHECK(disposition IN ('promoted','dismissed')),
    reason TEXT,
    destination_record_id TEXT REFERENCES provenance_records(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((disposition='promoted' AND destination_record_id IS NOT NULL AND reason IS NULL)
        OR (disposition='dismissed' AND destination_record_id IS NULL AND length(trim(reason)) > 0 AND reason IS NOT NULL))
);
CREATE TRIGGER observation_disposition_retain_source BEFORE DELETE ON task_context_items
WHEN EXISTS (SELECT 1 FROM observation_dispositions WHERE context_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'retain observation disposition source'); END;
CREATE TRIGGER observation_disposition_retain_capture BEFORE UPDATE ON task_context_items
WHEN EXISTS (SELECT 1 FROM observation_dispositions WHERE context_id=OLD.id)
AND (OLD.id IS NOT NEW.id OR OLD.content IS NOT NEW.content OR OLD.created_at IS NOT NEW.created_at
 OR OLD.skill_run_id IS NOT NEW.skill_run_id OR OLD.category IS NOT NEW.category
 OR OLD.file_hint IS NOT NEW.file_hint OR OLD.skill_hint IS NOT NEW.skill_hint
 OR OLD.item_type IS NOT NEW.item_type)
BEGIN SELECT RAISE(ABORT,'triaged observation capture is immutable'); END;
CREATE TRIGGER observation_disposition_state BEFORE UPDATE OF triage_status ON task_context_items
WHEN EXISTS (SELECT 1 FROM observation_dispositions WHERE context_id=OLD.id AND disposition IS NOT NEW.triage_status)
BEGIN SELECT RAISE(ABORT,'observation triage must match immutable disposition'); END;
CREATE TRIGGER observation_disposition_retain_alias BEFORE DELETE ON jot_aliases
WHEN EXISTS (SELECT 1 FROM observation_dispositions WHERE context_id=OLD.context_id)
BEGIN SELECT RAISE(ABORT,'retain observation disposition identity'); END;
CREATE TRIGGER observation_disposition_source BEFORE INSERT ON observation_dispositions
WHEN NOT EXISTS (SELECT 1 FROM task_context_items WHERE id=NEW.context_id AND item_type='observation' AND triage_status='pending')
BEGIN SELECT RAISE(ABORT,'disposition requires a pending observation'); END;
CREATE TRIGGER observation_disposition_no_replace BEFORE INSERT ON observation_dispositions
WHEN EXISTS (SELECT 1 FROM observation_dispositions WHERE context_id=NEW.context_id)
BEGIN SELECT RAISE(ABORT,'observation disposition already exists'); END;
CREATE TRIGGER observation_disposition_no_update BEFORE UPDATE ON observation_dispositions
BEGIN SELECT RAISE(ABORT,'observation dispositions are immutable'); END;
CREATE TRIGGER observation_disposition_no_delete BEFORE DELETE ON observation_dispositions
BEGIN SELECT RAISE(ABORT,'retain observation disposition history'); END;
"""


def details(conn, context_id):
    row = conn.execute('SELECT * FROM observation_dispositions WHERE context_id=?', (context_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    destination = result.pop('destination_record_id')
    p = tusk_loader.load('tusk-provenance')
    result['destination_ref'] = p.reference(p.project_id(conn), destination) if destination else None
    return result


def apply(conn, jot_id, *, destination=None, reason=None):
    """Bind an existing outcome; no destination creation is performed on retries."""
    p = tusk_loader.load('tusk-provenance')
    disposition = 'promoted' if destination is not None else 'dismissed'
    if disposition == 'dismissed':
        if reason is None or not reason.strip():
            raise ValueError('dismissal requires a nonblank --reason')
        reason = reason.strip()
    conn.execute('BEGIN IMMEDIATE')
    try:
        source = conn.execute("SELECT c.* FROM task_context_items c JOIN jot_aliases a ON a.context_id=c.id WHERE a.id=?", (jot_id,)).fetchone()
        if source is None:
            raise ValueError(f'jot {jot_id} not found')
        previous = details(conn, source['id'])
        if previous:
            if previous['disposition'] != disposition or previous['reason'] != reason or previous['destination_ref'] != destination:
                raise ValueError('observation already has a different disposition; inspect jots --triage-status all and reuse the recorded outcome')
            conn.commit()
            return {'id': jot_id, 'context_id': source['id'], 'triage_status': disposition, 'disposition': previous}
        if source['triage_status'] != 'pending':
            raise ValueError('observation is not pending; inspect its disposition history')
        dest = None
        if destination is not None:
            dest = p.fetch_record(conn, destination)
            if dest['availability'] != 'available' or dest['kind'] not in ('context','criterion','task'):
                raise ValueError('promotion requires an available decision/risk context, criterion, or task reference')
            table = p.NATIVE_V89[dest['kind']]
            target = conn.execute(f'SELECT * FROM {table} WHERE id=?', (dest['native_id'],)).fetchone()
            if target is None:
                raise ValueError('promotion destination no longer exists')
            if dest['kind'] == 'context' and (target['item_type'] not in ('decision','risk') or target['status'] != 'active'):
                raise ValueError('context promotion destination must be an active decision or risk')
            # Explicit destination refs may point to another task: retro can
            # legitimately add a criterion/risk to an existing follow-up task.
            context_ref = p.register(conn, 'context', source['id'])['ref']
            jot_ref = p.register(conn, 'jot', jot_id)['ref']
            p.link(conn, destination, 'derived_from', context_ref)
            p.link(conn, destination, 'derived_from', jot_ref)
        conn.execute('INSERT INTO observation_dispositions(context_id,disposition,reason,destination_record_id) VALUES (?,?,?,?)',
                     (source['id'], disposition, reason, dest['id'] if dest is not None else None))
        conn.execute("UPDATE task_context_items SET triage_status=?, updated_at=datetime('now') WHERE id=?", (disposition, source['id']))
        result = {'id': jot_id, 'context_id': source['id'], 'triage_status': disposition, 'disposition': details(conn, source['id'])}
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
