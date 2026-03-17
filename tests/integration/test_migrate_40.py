"""Integration test for migrate_40: trigger-only migration with stale config.

When migrate_40 runs against a DB at version 39 whose config.json is missing
'issue' in task_types, the migration must:
  1. Patch config.json to add 'issue' before regenerating triggers.
  2. Regenerate the validate_task_type trigger so it includes 'issue'.
  3. Advance user_version to 40.

This guards against the class of bug where regen_triggers is called before the
config is updated, which would leave the new enum value out of the trigger SQL.
"""

import importlib.util
import json
import os
import sqlite3

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "tusk_migrate",
        os.path.join(SCRIPT_DIR, "tusk-migrate.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_migrate = _load_migrate()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def stale_config(tmp_path):
    """Write a config.json that looks like a pre-migrate_40 install (no 'issue')."""
    src = os.path.join(REPO_ROOT, "config.default.json")
    with open(src) as f:
        cfg = json.load(f)

    # Remove 'issue' from task_types to simulate the stale state.
    cfg["task_types"] = [t for t in cfg.get("task_types", []) if t != "issue"]

    cfg_path = tmp_path / "config.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

    return str(cfg_path)


@pytest.fixture()
def db_at_v39(db_path):
    """Return a fully-initialised DB whose user_version has been rewound to 39."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 39")
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMigrate40StaleConfig:

    def test_config_patched_with_issue(self, db_at_v39, stale_config):
        """migrate_40 adds 'issue' to task_types in the config file."""
        with open(stale_config) as f:
            before = json.load(f)
        assert "issue" not in before.get("task_types", []), "pre-condition: config must not contain 'issue'"

        tusk_migrate.migrate_40(db_at_v39, stale_config, SCRIPT_DIR)

        with open(stale_config) as f:
            after = json.load(f)
        assert "issue" in after.get("task_types", []), "migrate_40 must add 'issue' to config task_types"

    def test_trigger_includes_issue(self, db_at_v39, stale_config):
        """After migrate_40, the validate_task_type trigger SQL contains 'issue'."""
        tusk_migrate.migrate_40(db_at_v39, stale_config, SCRIPT_DIR)

        conn = sqlite3.connect(db_at_v39)
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master"
            " WHERE type='trigger' AND name LIKE 'validate_task_type_%'"
        ).fetchall()
        conn.close()

        assert rows, "validate_task_type_insert/update triggers must exist after migration"
        for name, sql in rows:
            assert "issue" in sql, (
                f"Trigger '{name}' SQL should contain 'issue' after migration, got:\n{sql}"
            )

    def test_schema_version_advanced_to_40(self, db_at_v39, stale_config):
        """migrate_40 advances user_version to 40."""
        assert tusk_migrate.get_version(db_at_v39) == 39, "pre-condition: DB must be at version 39"

        tusk_migrate.migrate_40(db_at_v39, stale_config, SCRIPT_DIR)

        assert tusk_migrate.get_version(db_at_v39) == 40

    def test_idempotent_when_already_at_v40(self, db_path, stale_config):
        """migrate_40 is a no-op (aside from version stamp) when DB is already at v40."""
        # db_path is initialised at current schema version (40).
        assert tusk_migrate.get_version(str(db_path)) == 40

        tusk_migrate.migrate_40(str(db_path), stale_config, SCRIPT_DIR)

        # Version stays at 40 and no error is raised.
        assert tusk_migrate.get_version(str(db_path)) == 40
