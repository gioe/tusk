"""Integration test for migrate_48: collapse review.reviewers (array) → review.reviewer (object).

The fan-out reviewer array was removed in favour of a single reviewer object.
migrate_48 must rewrite an existing config.json so it stays parseable by the
new validator: take reviewers[0], strip its `domains` field, write it as
`review.reviewer`, and drop `review.reviewers`.

Empty arrays drop the key entirely (cmd_start falls back to an unassigned
review). Configs that already use the new schema (or have no review block)
are left alone aside from the version stamp.
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


def _write_config(tmp_path, review_block):
    cfg = {"task_types": ["bug"], "review": review_block}
    cfg_path = tmp_path / "config.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return str(cfg_path)


@pytest.fixture()
def db_at_v47(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 47")
    conn.commit()
    conn.close()
    return str(db_path)


class TestMigrate48:

    def test_collapses_first_reviewer_and_strips_domains(self, db_at_v47, tmp_path):
        cfg_path = _write_config(
            tmp_path,
            {
                "mode": "ai_only",
                "max_passes": 2,
                "reviewers": [
                    {"name": "general", "description": "All concerns", "domains": ["cli", "skills"]},
                    {"name": "security", "description": "Security only"},
                ],
            },
        )

        tusk_migrate.migrate_48(db_at_v47, cfg_path, SCRIPT_DIR)

        with open(cfg_path) as f:
            after = json.load(f)
        review = after["review"]
        assert "reviewers" not in review
        assert review["reviewer"] == {"name": "general", "description": "All concerns"}

    def test_empty_array_drops_key_without_setting_reviewer(self, db_at_v47, tmp_path):
        cfg_path = _write_config(
            tmp_path,
            {"mode": "ai_only", "max_passes": 2, "reviewers": []},
        )

        tusk_migrate.migrate_48(db_at_v47, cfg_path, SCRIPT_DIR)

        with open(cfg_path) as f:
            after = json.load(f)
        review = after["review"]
        assert "reviewers" not in review
        assert "reviewer" not in review

    def test_already_singular_config_is_left_alone(self, db_at_v47, tmp_path):
        cfg_path = _write_config(
            tmp_path,
            {"mode": "ai_only", "max_passes": 2, "reviewer": {"name": "general", "description": "..."}},
        )

        tusk_migrate.migrate_48(db_at_v47, cfg_path, SCRIPT_DIR)

        with open(cfg_path) as f:
            after = json.load(f)
        assert after["review"] == {"mode": "ai_only", "max_passes": 2, "reviewer": {"name": "general", "description": "..."}}

    def test_schema_version_advanced_to_48(self, db_at_v47, tmp_path):
        cfg_path = _write_config(tmp_path, {"mode": "ai_only", "reviewers": []})

        assert tusk_migrate.get_version(db_at_v47) == 47
        tusk_migrate.migrate_48(db_at_v47, cfg_path, SCRIPT_DIR)
        assert tusk_migrate.get_version(db_at_v47) == 48

    def test_idempotent_when_already_at_v48(self, db_path, tmp_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 48")
        conn.commit()
        conn.close()

        cfg_path = _write_config(
            tmp_path,
            {"mode": "ai_only", "reviewers": [{"name": "general", "description": "..."}]},
        )

        tusk_migrate.migrate_48(str(db_path), cfg_path, SCRIPT_DIR)

        # Already-migrated DB: version stays at 48 and config is not rewritten.
        assert tusk_migrate.get_version(str(db_path)) == 48
        with open(cfg_path) as f:
            after = json.load(f)
        assert "reviewers" in after["review"]
