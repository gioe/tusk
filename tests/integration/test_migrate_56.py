"""Integration test for migrate_56: recreate tasks-dependent views so ALTER
TABLE additions (e.g., fixes_task_id from migration 55) propagate into
``task_metrics``, ``v_ready_tasks``, ``v_chain_heads``, and
``v_criteria_coverage`` on DBs upgraded from v54.

Covers:
- schema version advances 55 → 56
- task_metrics, v_ready_tasks, v_chain_heads are recreated with the current
  tasks.* column list, so SELECT fixes_task_id FROM <view> succeeds (the
  exact regression cited in TASK-99's criterion 415)
- the four view definitions match bin/tusk's cmd_init verbatim (the
  canonical-source invariant)
- v_criteria_coverage's projected columns are unchanged (it never projected
  t.*, so column-list freezing never affected it; the migration still
  DROP+CREATEs it for uniformity)
- idempotent short-circuit on re-run against a fresh v56 install
"""

import importlib.util
import os
import re
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")
TUSK_BIN_PATH = os.path.join(SCRIPT_DIR, "tusk")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


@pytest.fixture()
def db_at_v55_with_pre_v55_views(db_path):
    """Simulate a DB that was migrated from v54 → v55.

    Fresh installs ship v56+ and rebuild views end-to-end in ``cmd_init``, so
    ``fixes_task_id`` is already present in ``task_metrics``. To reproduce
    the migrated-DB trap, manually recreate the three ``SELECT t.*`` views
    with the pre-v55 tasks column list (i.e. every tasks column *except*
    ``fixes_task_id``) and stamp the DB back to version 55. Under that
    shape, ``SELECT fixes_task_id FROM task_metrics`` fails with
    ``no such column`` — exactly the regression migration 56 fixes.
    """
    db = str(db_path)
    conn = sqlite3.connect(db)

    cols = [
        r[1]
        for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        if r[1] != "fixes_task_id"
    ]
    projection = ", ".join(f't."{c}"' for c in cols)

    conn.executescript(
        f"""
        DROP VIEW IF EXISTS task_metrics;
        CREATE VIEW task_metrics AS
        SELECT {projection},
            COUNT(s.id) as session_count,
            SUM(s.duration_seconds) as total_duration_seconds,
            SUM(s.cost_dollars) as total_cost,
            SUM(s.tokens_in) as total_tokens_in,
            SUM(s.tokens_out) as total_tokens_out,
            SUM(s.lines_added) as total_lines_added,
            SUM(s.lines_removed) as total_lines_removed,
            SUM(s.request_count) as total_request_count,
            (SELECT COUNT(*) FROM task_status_transitions tst
              WHERE tst.task_id = t.id AND tst.to_status = 'To Do') as reopen_count
        FROM tasks t
        LEFT JOIN task_sessions s ON t.id = s.task_id
        GROUP BY t.id;

        DROP VIEW IF EXISTS v_ready_tasks;
        CREATE VIEW v_ready_tasks AS
        SELECT {projection}
        FROM tasks t
        WHERE t.status = 'To Do'
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );

        DROP VIEW IF EXISTS v_chain_heads;
        CREATE VIEW v_chain_heads AS
        SELECT {projection}
        FROM tasks t
        WHERE t.status <> 'Done'
          AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks downstream ON d.task_id = downstream.id
            WHERE d.depends_on_id = t.id AND downstream.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on_id = blocker.id
            WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
          )
          AND NOT EXISTS (
            SELECT 1 FROM external_blockers eb
            WHERE eb.task_id = t.id AND eb.is_resolved = 0
          );
        """
    )
    conn.execute("PRAGMA user_version = 55")
    conn.commit()
    conn.close()
    return db


def _view_sql(db, name):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (name,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _canonical_view_sql(view_name):
    """Extract the canonical CREATE VIEW <view_name> block from bin/tusk."""
    with open(TUSK_BIN_PATH, "r") as f:
        content = f.read()
    # Match "CREATE VIEW <name> AS ... ;" non-greedily up to the first ';'
    # followed by a blank line or another CREATE/DROP. Views in cmd_init are
    # each terminated by a ';' on its own closing line; restrict the match
    # to the first ';' at column 0 or after GROUP BY to stay safe.
    pattern = re.compile(
        r"CREATE VIEW " + re.escape(view_name) + r"\b.*?;",
        re.DOTALL,
    )
    m = pattern.search(content)
    return m.group(0) if m else None


class TestMigrate56:

    def test_advances_schema_version_to_56(
        self, db_at_v55_with_pre_v55_views, config_path
    ):
        assert tusk_migrate.get_version(db_at_v55_with_pre_v55_views) == 55
        tusk_migrate.migrate_56(db_at_v55_with_pre_v55_views, config_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v55_with_pre_v55_views) == 56

    def test_task_metrics_exposes_fixes_task_id_after_migrate(
        self, db_at_v55_with_pre_v55_views, config_path
    ):
        """Criterion 415: on a DB migrated from v54, SELECT fixes_task_id FROM
        task_metrics must fail before migrate_56 and succeed after."""
        conn = sqlite3.connect(db_at_v55_with_pre_v55_views)
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            conn.execute("SELECT fixes_task_id FROM task_metrics LIMIT 1").fetchone()
        conn.close()

        tusk_migrate.migrate_56(db_at_v55_with_pre_v55_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v55_with_pre_v55_views)
        # Must not raise.
        conn.execute("SELECT fixes_task_id FROM task_metrics LIMIT 1").fetchone()
        conn.close()

    def test_v_ready_tasks_and_v_chain_heads_expose_fixes_task_id(
        self, db_at_v55_with_pre_v55_views, config_path
    ):
        """The other two SELECT t.* views are recreated in the same pass."""
        tusk_migrate.migrate_56(db_at_v55_with_pre_v55_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v55_with_pre_v55_views)
        # Neither must raise.
        conn.execute("SELECT fixes_task_id FROM v_ready_tasks LIMIT 1").fetchone()
        conn.execute("SELECT fixes_task_id FROM v_chain_heads LIMIT 1").fetchone()
        conn.close()

    def test_v_criteria_coverage_projects_unchanged_columns(
        self, db_at_v55_with_pre_v55_views, config_path
    ):
        """v_criteria_coverage never projected t.*, so its column list does
        not freeze against tasks ALTER TABLE. The migration still DROPs and
        re-CREATEs it for uniformity; the resulting columns must remain
        task_id, summary, total_criteria, completed_criteria,
        remaining_criteria."""
        tusk_migrate.migrate_56(db_at_v55_with_pre_v55_views, config_path, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v55_with_pre_v55_views)
        cols = [
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(v_criteria_coverage)"
            ).fetchall()
        ]
        conn.close()

        assert cols == [
            "task_id",
            "summary",
            "total_criteria",
            "completed_criteria",
            "remaining_criteria",
        ]

    def test_view_definitions_match_cmd_init(
        self, db_at_v55_with_pre_v55_views, config_path
    ):
        """Each recreated view's stored SQL must match the canonical
        definition in cmd_init (whitespace-normalized). This guards against
        drift between the migration's embedded SQL and the fresh-install
        schema."""
        tusk_migrate.migrate_56(db_at_v55_with_pre_v55_views, config_path, SCRIPT_DIR)

        def _normalize(sql):
            return re.sub(r"\s+", " ", sql).strip().rstrip(";")

        for view in ("task_metrics", "v_ready_tasks", "v_chain_heads", "v_criteria_coverage"):
            db_sql = _view_sql(db_at_v55_with_pre_v55_views, view)
            canonical = _canonical_view_sql(view)
            assert db_sql is not None, f"{view} missing after migrate_56"
            assert canonical is not None, f"canonical {view} not found in bin/tusk"
            assert _normalize(db_sql) == _normalize(canonical), (
                f"{view} definition drifted from cmd_init"
            )

    def test_idempotent_when_already_at_v56(self, db_path, config_path):
        """Fresh DB ships at v56+. Stamping to 56 explicitly keeps the test
        future-proof across later migrations; migrate_56 must short-circuit
        without touching any view or bumping the version."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 56")
        conn.commit()
        conn.close()

        before = _view_sql(str(db_path), "task_metrics")
        version_before = tusk_migrate.get_version(str(db_path))

        tusk_migrate.migrate_56(str(db_path), config_path, SCRIPT_DIR)

        assert tusk_migrate.get_version(str(db_path)) == version_before
        assert _view_sql(str(db_path), "task_metrics") == before
