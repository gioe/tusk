from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FETCH_BOOTSTRAP = REPO_ROOT / "bin" / "tusk-init-fetch-bootstrap.py"
SELECT_BOOTSTRAP = REPO_ROOT / "bin" / "tusk-init-bootstrap-select.py"
PACK_ROOT = REPO_ROOT / "docs" / "bootstrap-packs"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_ios_libs_example_manifest_validates_against_bootstrap_schema():
    mod = _load_module(FETCH_BOOTSTRAP, "tusk_init_fetch_bootstrap_examples")
    manifest_path = PACK_ROOT / "ios-libs" / "tusk-bootstrap.json"

    manifest = _load_json(manifest_path)

    assert mod._validate(manifest) is None
    assert manifest["project_type"] == "ios_app"
    assert manifest["manifest_schema_version"] == 2


def test_ios_libs_example_exercises_rich_modular_schema():
    manifest = _load_json(PACK_ROOT / "ios-libs" / "tusk-bootstrap.json")
    modules = {module["id"]: module for module in manifest["modules"]}

    expected_modules = {
        "sharedkit-design-system",
        "api-client",
        "navigation-shell",
        "persistence",
        "observability",
        "test-scaffolding",
    }
    assert expected_modules <= set(modules)

    sharedkit = modules["sharedkit-design-system"]
    assert sharedkit["files"]
    assert sharedkit["tasks"]
    assert sharedkit["context_atoms"]
    assert sharedkit["verification_hints"]

    all_file_specs = []
    for module in modules.values():
        all_file_specs.extend(module.get("files", []))
        all_file_specs.extend(module.get("optional_files", []))
        all_file_specs.extend(module.get("append_operations", []))

    assert any(spec.get("mode") == "create_only" for spec in all_file_specs)
    assert any(spec.get("mode") == "append_if_missing" for spec in all_file_specs)
    assert any(spec.get("mode") == "marker_block" for spec in all_file_specs)


def test_future_repo_placeholder_contracts_document_expected_pack_shape():
    for name, project_type in (
        ("android-libs", "android_app"),
        ("web-libs", "web_app"),
        ("backend-libs", "backend"),
    ):
        text = (PACK_ROOT / name / "CONTRACT.md").read_text(encoding="utf-8")
        assert "tusk-bootstrap.json" in text
        assert project_type in text
        assert "manifest_schema_version" in text
        assert "modules" in text
        assert "verification_hints" in text


def test_unavailable_future_repos_remain_optional_selector_skips():
    mod = _load_module(SELECT_BOOTSTRAP, "tusk_init_bootstrap_select_examples")

    android = mod.select_bootstrap_packs(
        project_type="android_app",
        intent={"platforms": ["android"], "stack_preferences": ["Kotlin", "Compose"]},
    )
    web = mod.select_bootstrap_packs(
        project_type="web_app",
        intent={"platforms": ["web"], "primary_workflows": ["dashboard"]},
        archetype={"id": "b2b_dashboard"},
    )
    backend = mod.select_bootstrap_packs(
        project_type="python_service",
        intent={"platforms": ["backend"], "integrations": ["database"]},
        archetype={"id": "api_service"},
    )

    assert android["skipped_modules"] == [
        {"name": "android_app", "reason": "optional utility repo is not configured"}
    ]
    assert any(item["name"] == "web_app" for item in web["skipped_modules"])
    assert any(item["name"] == "backend" for item in backend["skipped_modules"])
