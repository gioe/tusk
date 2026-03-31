"""Unit tests for migration_hints validation in tusk-init-fetch-bootstrap.py.

Covers the three new paths added to _validate():
  1. task with migration_hints as a valid list of strings → passes
  2. task with migration_hints as a non-list value → fails
  3. task with migration_hints as a list containing a non-string element → fails
"""

import importlib.util
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-fetch-bootstrap.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_fetch_bootstrap", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid_data(**overrides):
    """Return a minimal valid bootstrap payload, with optional task-level overrides."""
    task = {
        "summary": "Do something",
        "description": "Details here",
        "priority": "Medium",
        "task_type": "feature",
        "complexity": "S",
        "criteria": ["It works"],
    }
    task.update(overrides)
    return {
        "version": 1,
        "project_type": "ios_app",
        "tasks": [task],
    }


class TestValidateMigrationHints:
    def test_valid_migration_hints_string_array_passes(self):
        """migration_hints as a list of strings should not cause a validation error."""
        mod = _load_module()
        data = _valid_data(migration_hints=["Run migration A", "Check table B"])
        assert mod._validate(data) is None

    def test_migration_hints_non_list_fails(self):
        """migration_hints set to a non-list value should return an error string."""
        mod = _load_module()
        data = _valid_data(migration_hints="run migration")
        result = mod._validate(data)
        assert result is not None
        assert "migration_hints" in result

    def test_migration_hints_list_with_non_string_element_fails(self):
        """migration_hints containing a non-string element should return an error string."""
        mod = _load_module()
        data = _valid_data(migration_hints=["valid hint", 42])
        result = mod._validate(data)
        assert result is not None
        assert "migration_hints" in result
