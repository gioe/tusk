"""Unit tests for the bootstrap validator in tusk-init-fetch-bootstrap.py.

Covers two extension blocks added to _validate():
  - migration_hints (per-task, optional list of strings)
  - manifest_files (top-level, optional list of {path, content, mode?} objects)
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


def _with_manifest(manifest_files):
    """Return a minimal valid bootstrap payload with the given manifest_files block."""
    base = _valid_data()
    base["manifest_files"] = manifest_files
    return base


class TestValidateManifestFiles:
    def test_absent_manifest_files_passes(self):
        """manifest_files is optional; omitting it should not fail validation."""
        mod = _load_module()
        assert mod._validate(_valid_data()) is None

    def test_valid_manifest_files_default_mode_passes(self):
        """Entry with path + content (no mode) should accept the create_only default."""
        mod = _load_module()
        data = _with_manifest([{"path": "Package.swift", "content": "// swift"}])
        assert mod._validate(data) is None

    def test_valid_manifest_files_append_mode_passes(self):
        """append_if_missing is a valid mode value."""
        mod = _load_module()
        data = _with_manifest([
            {"path": "requirements.txt", "content": "gioe-libs\n", "mode": "append_if_missing"},
        ])
        assert mod._validate(data) is None

    def test_manifest_files_non_list_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest("not-a-list"))
        assert result is not None
        assert "manifest_files" in result
        assert "array" in result

    def test_manifest_files_non_dict_entry_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest(["just a string"]))
        assert result is not None
        assert "manifest_files[0]" in result

    def test_missing_path_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([{"content": "x"}]))
        assert result is not None
        assert "path" in result
        assert "manifest_files[0]" in result

    def test_empty_path_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "", "content": "x"}]))
        assert result is not None
        assert "path" in result

    def test_absolute_path_fails(self):
        """Absolute paths must be rejected — this would let a lib write anywhere on disk."""
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "/etc/passwd", "content": "x"}]))
        assert result is not None
        assert "absolute" in result

    def test_path_traversal_leading_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "../etc/passwd", "content": "x"}]))
        assert result is not None
        assert ".." in result

    def test_path_traversal_embedded_fails(self):
        """A '..' segment anywhere in the path must be rejected, even mid-path."""
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "ios/../../etc", "content": "x"}]))
        assert result is not None
        assert ".." in result

    def test_path_invalid_chars_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "foo bar.txt", "content": "x"}]))
        assert result is not None
        assert "invalid characters" in result

    def test_missing_content_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "a.txt"}]))
        assert result is not None
        assert "content" in result

    def test_non_string_content_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([{"path": "a.txt", "content": 123}]))
        assert result is not None
        assert "content" in result

    def test_unknown_mode_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([
            {"path": "a.txt", "content": "x", "mode": "overwrite"},
        ]))
        assert result is not None
        assert "mode" in result
        assert "create_only" in result
        assert "append_if_missing" in result
