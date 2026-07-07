"""Unit tests for the bootstrap validator in tusk-init-fetch-bootstrap.py.

Covers two extension blocks added to _validate():
  - migration_hints (per-task, optional list of strings)
  - manifest_files (top-level, optional list of {path, content, mode?} objects)
"""

import importlib.util
import json
import os
import sys

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


def _valid_module(**overrides):
    module = {
        "id": "sharedkit",
        "name": "SharedKit",
        "description": "Shared SwiftUI design tokens and base components.",
        "applicability": {
            "project_types": ["ios_app"],
            "archetypes": ["consumer_ios_app"],
            "platforms": ["ios"],
            "requires": ["SwiftUI"],
        },
        "files": [
            {"path": "Package.swift", "content": "// package\n", "mode": "create_only"},
        ],
        "optional_files": [
            {"path": "README.md", "content": "# SharedKit\n"},
        ],
        "append_operations": [
            {"path": ".gitignore", "content": ".build/\n"},
        ],
        "dependencies": ["api-client"],
        "pillars": [
            {"name": "Native feel", "claim": "Use platform conventions before custom UI."},
        ],
        "glossary": [
            {"term": "Design token", "definition": "A named reusable UI value."},
        ],
        "context_atoms": [
            {"type": "decision", "content": "Use SharedKit for core UI primitives."},
        ],
        "tasks": [
            {
                "summary": "Add SharedKit",
                "description": "Add the SharedKit package and wire base tokens.",
                "priority": "High",
                "task_type": "feature",
                "complexity": "S",
                "criteria": ["SharedKit is importable"],
            },
        ],
        "verification_hints": [
            "Run swift test after adding the package.",
        ],
    }
    module.update(overrides)
    return module


def _rich_data(modules):
    data = _valid_data()
    data["manifest_schema_version"] = 2
    data["modules"] = modules
    return data


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

    def test_valid_manifest_files_marker_block_passes(self):
        """marker_block is valid when both marker strings are present."""
        mod = _load_module()
        data = _with_manifest([
            {
                "path": "Package.swift",
                "content": ".package(url: \"https://example.com/lib\", from: \"1.0.0\")\n",
                "mode": "marker_block",
                "begin_marker": "// BEGIN TUSK",
                "end_marker": "// END TUSK",
            },
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

    def test_marker_block_missing_begin_marker_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([
            {
                "path": "Package.swift",
                "content": "x",
                "mode": "marker_block",
                "end_marker": "// END TUSK",
            },
        ]))
        assert result is not None
        assert "begin_marker" in result

    def test_marker_block_missing_end_marker_fails(self):
        mod = _load_module()
        result = mod._validate(_with_manifest([
            {
                "path": "Package.swift",
                "content": "x",
                "mode": "marker_block",
                "begin_marker": "// BEGIN TUSK",
            },
        ]))
        assert result is not None
        assert "end_marker" in result


class TestValidateBootstrapModules:
    def test_task_only_manifest_remains_valid_without_modules(self):
        mod = _load_module()
        data = _valid_data()

        assert mod._validate(data) is None

    def test_valid_rich_module_manifest_passes(self):
        mod = _load_module()
        data = _rich_data([_valid_module()])

        assert mod._validate(data) is None

    def test_manifest_schema_version_must_be_integer_when_present(self):
        mod = _load_module()
        data = _rich_data([_valid_module()])
        data["manifest_schema_version"] = "2"
        result = mod._validate(data)

        assert result is not None
        assert "manifest_schema_version" in result

    def test_modules_must_be_an_array(self):
        mod = _load_module()
        result = mod._validate(_rich_data("not-a-list"))

        assert result is not None
        assert "modules must be an array" in result

    def test_module_missing_required_metadata_fails_with_path(self):
        mod = _load_module()
        module = _valid_module()
        del module["id"]
        result = mod._validate(_rich_data([module]))

        assert result is not None
        assert "modules[0]" in result
        assert "id" in result

    def test_module_applicability_values_must_be_string_arrays(self):
        mod = _load_module()
        module = _valid_module(applicability={"project_types": "ios_app"})
        result = mod._validate(_rich_data([module]))

        assert result is not None
        assert "modules[0].applicability.project_types" in result
        assert "array of strings" in result

    def test_module_files_reuse_manifest_file_validation(self):
        mod = _load_module()
        module = _valid_module(files=[{"path": "../Package.swift", "content": "x"}])
        result = mod._validate(_rich_data([module]))

        assert result is not None
        assert "modules[0].files[0].path" in result
        assert ".." in result

    def test_module_tasks_reuse_task_validation(self):
        mod = _load_module()
        task = _valid_module()["tasks"][0]
        del task["criteria"]
        module = _valid_module(tasks=[task])
        result = mod._validate(_rich_data([module]))

        assert result is not None
        assert "modules[0].tasks[0]" in result
        assert "criteria" in result

    def test_main_returns_modules_for_valid_rich_manifest(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"project_libs": {"ios_app": {"repo": "owner/ios-libs", "ref": "main"}}})
        )
        data = _rich_data([_valid_module()])

        monkeypatch.setattr(mod, "_fetch_bootstrap", lambda repo, ref: (data, None))
        monkeypatch.setattr(sys, "argv", ["script", "tasks.db", str(config_path)])

        mod.main()

        payload = json.loads(capsys.readouterr().out)
        lib = payload["libs"][0]
        assert lib["error"] is None
        assert lib["tasks"] == data["tasks"]
        assert lib["modules"] == data["modules"]
        assert lib["manifest_schema_version"] == 2

    def test_main_returns_actionable_error_for_invalid_module(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"project_libs": {"ios_app": {"repo": "owner/ios-libs", "ref": "main"}}})
        )
        module = _valid_module(files=[{"path": "/tmp/bad", "content": "x"}])
        data = _rich_data([module])

        monkeypatch.setattr(mod, "_fetch_bootstrap", lambda repo, ref: (data, None))
        monkeypatch.setattr(sys, "argv", ["script", "tasks.db", str(config_path)])

        mod.main()

        payload = json.loads(capsys.readouterr().out)
        lib = payload["libs"][0]
        assert lib["tasks"] == []
        assert lib["modules"] == []
        assert "invalid bootstrap" in lib["error"]
        assert "modules[0].files[0].path" in lib["error"]
