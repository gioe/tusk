"""Schema 93: observation atoms with stable compatibility identities for jots.

Only task_context_items owns observation content. jot_aliases is an identity
index, and jots is the SQL/CLI compatibility projection of that storage.
"""

import re
import sqlite3

CONTEXT_DDL = """
CREATE TABLE task_context_items_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    objective_id INTEGER,
    item_type TEXT NOT NULL CHECK (item_type IN ('memory','assumption','question','risk','decision','entry_point','observation')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved','superseded')),
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','create_task','task_progress','review','retro','agent_handoff')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    skill_run_id INTEGER,
    category TEXT,
    file_hint TEXT,
    skill_hint TEXT,
    triage_status TEXT CHECK (triage_status IN ('pending','promoted','dismissed')),
    CHECK ((item_type = 'observation' AND skill_run_id IS NOT NULL AND category IS NOT NULL AND triage_status IS NOT NULL)
        OR (item_type <> 'observation' AND task_id IS NOT NULL AND skill_run_id IS NULL AND category IS NULL
            AND file_hint IS NULL AND skill_hint IS NULL AND triage_status IS NULL)),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL,
    FOREIGN KEY (objective_id) REFERENCES objectives(id) ON DELETE SET NULL,
    FOREIGN KEY (skill_run_id) REFERENCES skill_runs(id) ON DELETE CASCADE
);
"""

COMPAT_DDL = """
CREATE INDEX idx_task_context_items_task_id ON task_context_items(task_id);
CREATE INDEX idx_task_context_items_objective_id ON task_context_items(objective_id);
CREATE INDEX idx_task_context_items_type_status ON task_context_items(item_type,status);
CREATE INDEX idx_observation_skill_run ON task_context_items(skill_run_id) WHERE item_type='observation';
CREATE INDEX idx_observation_category_triage ON task_context_items(category,triage_status) WHERE item_type='observation';
CREATE TABLE jot_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id INTEGER NOT NULL UNIQUE REFERENCES task_context_items(id) ON DELETE CASCADE
);
CREATE TRIGGER observation_alias_delete BEFORE DELETE ON task_context_items BEGIN
    DELETE FROM jot_aliases WHERE context_id=OLD.id;
END;
CREATE TRIGGER observation_alias_context_identity BEFORE UPDATE OF id ON task_context_items
WHEN OLD.id IS NOT NEW.id AND EXISTS (SELECT 1 FROM jot_aliases WHERE context_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'observation identity is immutable'); END;
CREATE TRIGGER observation_task_delete BEFORE DELETE ON tasks BEGIN
    DELETE FROM task_context_items WHERE task_id=OLD.id AND item_type <> 'observation';
END;
CREATE TRIGGER jot_alias_no_rebind BEFORE INSERT ON jot_aliases
WHEN EXISTS (SELECT 1 FROM jot_aliases WHERE context_id=NEW.context_id)
BEGIN SELECT RAISE(ABORT,'observation already has a jot identity'); END;
CREATE TRIGGER jot_alias_observation_insert BEFORE INSERT ON jot_aliases
WHEN NOT EXISTS (SELECT 1 FROM task_context_items WHERE id=NEW.context_id AND item_type='observation')
BEGIN SELECT RAISE(ABORT,'jot alias requires an observation'); END;
CREATE TRIGGER jot_alias_identity_update BEFORE UPDATE ON jot_aliases
WHEN OLD.context_id IS NOT NEW.context_id OR OLD.id IS NOT NEW.id
BEGIN SELECT RAISE(ABORT,'jot alias identity is immutable'); END;
CREATE TRIGGER observation_type_identity BEFORE UPDATE OF item_type ON task_context_items
WHEN OLD.item_type IS NOT NEW.item_type AND (OLD.item_type='observation' OR NEW.item_type='observation')
BEGIN SELECT RAISE(ABORT,'observation identity is immutable'); END;
CREATE VIEW jots AS
SELECT a.id,c.skill_run_id,c.task_id,c.category,c.content AS note,c.file_hint,c.skill_hint,c.created_at
FROM jot_aliases a JOIN task_context_items c ON c.id=a.context_id;
CREATE TRIGGER jots_insert INSTEAD OF INSERT ON jots BEGIN
    SELECT CASE WHEN NEW.id IS NOT NULL AND EXISTS (SELECT 1 FROM jot_aliases WHERE id=NEW.id)
        THEN RAISE(ABORT,'jot identity already exists') END;
    INSERT INTO task_context_items (task_id,item_type,content,skill_run_id,category,file_hint,skill_hint,triage_status,created_at,updated_at)
    VALUES (NEW.task_id,'observation',NEW.note,NEW.skill_run_id,NEW.category,NEW.file_hint,NEW.skill_hint,'pending',
            COALESCE(NEW.created_at,datetime('now')),COALESCE(NEW.created_at,datetime('now')));
    INSERT INTO jot_aliases (id,context_id) VALUES (NEW.id,last_insert_rowid());
END;
CREATE TRIGGER jots_update INSTEAD OF UPDATE ON jots BEGIN
    SELECT CASE WHEN OLD.id IS NOT NEW.id THEN RAISE(ABORT,'jot alias identity is immutable') END;
    UPDATE task_context_items SET task_id=NEW.task_id,skill_run_id=NEW.skill_run_id,category=NEW.category,
        content=NEW.note,file_hint=NEW.file_hint,skill_hint=NEW.skill_hint,created_at=NEW.created_at,
        updated_at=datetime('now') WHERE id=(SELECT context_id FROM jot_aliases WHERE id=OLD.id);
END;
CREATE TRIGGER jots_delete INSTEAD OF DELETE ON jots BEGIN
    DELETE FROM task_context_items WHERE id=(SELECT context_id FROM jot_aliases WHERE id=OLD.id);
END;
"""


def upgrade_schema(conn):
    """Convert v92 atomically, serializing metadata reads with concurrent writes."""
    if conn.in_transaction:
        raise ValueError('observation migration requires a transaction boundary')
    fk = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    try:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('PRAGMA user_version').fetchone()[0] < 93:
            _upgrade_locked(conn)
        # Validate the storage this migration owns. Other historical tables
        # may already contain unrelated dangling rows; they cannot make a
        # lossless observation upgrade fail.
        violations = []
        for table in ('task_context_items', 'jot_aliases'):
            violations.extend(conn.execute(f'PRAGMA foreign_key_check({table})').fetchall())
        if violations:
            raise ValueError('observation migration encountered foreign-key violations')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f'PRAGMA foreign_keys={int(fk)}')


def _upgrade_locked(conn):
    # Restore dependent triggers only after both replacement endpoints exist.
    triggers = list(conn.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"))
    affected = [r for r in triggers if re.search(r'\b(task_context_items|jots)\b', r[2], re.I)]
    context_seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='task_context_items'").fetchone()
    jot_seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='jots'").fetchone()
    context_columns = 'id,task_id,objective_id,item_type,content,status,source,created_at,updated_at,resolved_at'
    sql = ''
    for name, _, _ in affected:
        sql += 'DROP TRIGGER "' + name.replace('"','""') + '";\n'
    sql += CONTEXT_DDL
    sql += f'INSERT INTO task_context_items_new ({context_columns}) SELECT {context_columns} FROM task_context_items;\n'
    sql += 'DROP TABLE task_context_items;\nALTER TABLE task_context_items_new RENAME TO task_context_items;\n'
    # Keep the old jot rows available during conversion; no trigger may refer
    # to the old name while SQLite performs this rename.
    sql += 'ALTER TABLE jots RENAME TO legacy_jots_v93;\n' + COMPAT_DDL
    if context_seq:
        sql += f"UPDATE sqlite_sequence SET seq=MAX(seq,{int(context_seq[0])}) WHERE name='task_context_items';\n"
        sql += f"INSERT INTO sqlite_sequence(name,seq) SELECT 'task_context_items',{int(context_seq[0])} WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='task_context_items');\n"
    # A temporary mapping makes colliding legacy context/jot IDs independent.
    sql += """
CREATE TEMP TABLE observation_migration_map (jot_id INTEGER,context_id INTEGER);
INSERT INTO observation_migration_map SELECT id,
    (SELECT COALESCE(MAX(seq),0) FROM sqlite_sequence WHERE name='task_context_items') + ROW_NUMBER() OVER (ORDER BY id)
    FROM legacy_jots_v93;
INSERT INTO task_context_items (id,task_id,item_type,content,created_at,updated_at,skill_run_id,category,file_hint,skill_hint,triage_status)
SELECT m.context_id,j.task_id,'observation',j.note,j.created_at,j.created_at,j.skill_run_id,j.category,j.file_hint,j.skill_hint,'pending'
FROM legacy_jots_v93 j JOIN observation_migration_map m ON m.jot_id=j.id;
INSERT INTO jot_aliases(id,context_id) SELECT jot_id,context_id FROM observation_migration_map;
DROP TABLE observation_migration_map;
DROP TABLE legacy_jots_v93;
"""
    if jot_seq:
        sql += f"UPDATE sqlite_sequence SET seq=MAX(seq,{int(jot_seq[0])}) WHERE name='jot_aliases';\n"
        sql += f"INSERT INTO sqlite_sequence(name,seq) SELECT 'jot_aliases',{int(jot_seq[0])} WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='jot_aliases');\n"
    for _, table, ddl in affected:
        # The alias row owns the stable jot native ID. Existence checks on
        # provenance_records continue querying the compatibility projection.
        if table == 'jots':
            ddl = re.sub(r'\bjots\b','jot_aliases',ddl)
        sql += ddl + ';\n'
    sql += 'PRAGMA user_version=93;'
    # executescript implicitly commits an existing transaction. Execute whole
    # statements individually instead so the writer lock spans reads and DDL.
    statement = ''
    for char in sql:
        statement += char
        if char == ';' and sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('incomplete observation migration SQL')


def insert_observation(conn, *, skill_run_id, task_id, category, note, file_hint=None, skill_hint=None):
    """Write atom and compatibility identity within the caller's transaction."""
    cursor = conn.execute(
        "INSERT INTO task_context_items (task_id,item_type,content,skill_run_id,category,file_hint,skill_hint,triage_status) "
        "VALUES (?,'observation',?,?,?,?,?,'pending')",
        (task_id,note,skill_run_id,category,file_hint,skill_hint),
    )
    alias = conn.execute('INSERT INTO jot_aliases(context_id) VALUES (?)',(cursor.lastrowid,))
    return alias.lastrowid
