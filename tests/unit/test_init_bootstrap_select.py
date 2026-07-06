from __future__ import annotations

import importlib.util
import json
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-bootstrap-select.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_bootstrap_select", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_selects_single_ios_pack_from_project_type_and_platform():
    mod = _load_module()

    result = mod.select_bootstrap_packs(
        project_type="ios_app",
        intent={"platforms": ["ios"], "stack_preferences": ["SwiftUI"]},
        archetype={"id": "consumer_ios_app"},
    )

    assert result["project_libs"] == {"ios_app": {"repo": "gioe/ios-libs", "ref": "main"}}
    assert result["selected_modules"][0]["name"] == "ios_app"
    assert "project_type=ios_app" in result["selected_modules"][0]["matched"]


def test_selects_multiple_packs_from_archetype_features_and_platforms():
    mod = _load_module()
    catalog = {
        "web_app": {
            "repo": "gioe/web-libs",
            "ref": "main",
            "applicability": {
                "archetypes": ["b2b_dashboard"],
                "platforms": ["web"],
                "features": ["dashboard"],
            },
        },
        "backend": {
            "repo": "gioe/backend-libs",
            "ref": "main",
            "applicability": {
                "archetypes": ["b2b_dashboard"],
                "features": ["api", "auth"],
            },
        },
    }

    result = mod.select_bootstrap_packs(
        project_type="web_app",
        intent={"platforms": ["web"], "integrations": ["Okta"]},
        archetype={"id": "b2b_dashboard"},
        selected_features=["dashboard", "auth"],
        catalog=catalog,
    )

    assert result["project_libs"] == {
        "web_app": {"repo": "gioe/web-libs", "ref": "main"},
        "backend": {"repo": "gioe/backend-libs", "ref": "main"},
    }
    assert [m["name"] for m in result["selected_modules"]] == ["web_app", "backend"]


def test_missing_optional_pack_is_skipped_not_selected():
    mod = _load_module()
    catalog = {
        "android_app": {
            "repo": None,
            "ref": "main",
            "optional": True,
            "applicability": {"project_types": ["android_app"], "platforms": ["android"]},
        }
    }

    result = mod.select_bootstrap_packs(
        project_type="android_app",
        intent={"platforms": ["android"]},
        catalog=catalog,
    )

    assert result["project_libs"] == {}
    assert result["selected_modules"] == []
    assert result["skipped_modules"] == [
        {"name": "android_app", "reason": "optional utility repo is not configured"}
    ]


def test_existing_project_type_defaults_remain_backward_compatible():
    mod = _load_module()

    ios = mod.select_bootstrap_packs(project_type="ios_app")
    python = mod.select_bootstrap_packs(project_type="python_service")

    assert ios["project_libs"]["ios_app"] == {"repo": "gioe/ios-libs", "ref": "main"}
    assert python["project_libs"]["python_service"] == {"repo": "gioe/python-libs", "ref": "main"}


def test_cli_outputs_selection_json(capsys):
    mod = _load_module()
    rc = mod.main(
        [
            "ignored.db",
            "ignored-config.json",
            "--project-type",
            "ios_app",
            "--intent",
            json.dumps({"platforms": ["ios"]}),
            "--archetype",
            json.dumps({"id": "consumer_ios_app"}),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert "ios_app" in payload["selection"]["project_libs"]
