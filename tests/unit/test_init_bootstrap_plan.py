from __future__ import annotations

import importlib.util
import json
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-bootstrap-plan.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_bootstrap_plan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bootstrap():
    return {
        "libs": [
            {
                "name": "ios_app",
                "repo": "gioe/ios-libs",
                "manifest_schema_version": 2,
                "error": None,
                "manifest_files": [
                    {"path": "Package.swift", "content": "// package\n", "mode": "create_only"}
                ],
                "modules": [
                    {
                        "id": "sharedkit",
                        "name": "SharedKit",
                        "description": "Shared SwiftUI tokens.",
                        "applicability": {
                            "project_types": ["ios_app"],
                            "archetypes": ["consumer_ios_app"],
                            "platforms": ["ios"],
                            "requires": ["SwiftUI"],
                        },
                        "files": [
                            {"path": "Sources/SharedKit.swift", "content": "// shared\n"}
                        ],
                        "context_atoms": [
                            {"type": "decision", "content": "Use SharedKit for UI primitives."}
                        ],
                        "pillars": [
                            {"name": "Native feel", "claim": "Prefer platform conventions."}
                        ],
                        "tasks": [
                            {
                                "summary": "Wire SharedKit",
                                "description": "Use SharedKit in the app shell.",
                                "priority": "Medium",
                                "task_type": "feature",
                                "complexity": "M",
                                "criteria": ["SharedKit is imported"],
                            }
                        ],
                    },
                    {
                        "id": "android-only",
                        "name": "Android only",
                        "description": "Not relevant.",
                        "applicability": {"platforms": ["android"]},
                    },
                ],
                "tasks": [
                    {
                        "summary": "Add iOS lib",
                        "description": "Add the package dependency.",
                        "priority": "Medium",
                        "task_type": "feature",
                        "complexity": "S",
                        "criteria": ["Package resolves"],
                    }
                ],
            }
        ]
    }


def test_plan_selects_applicable_modules_and_explains_reasons():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "platforms": ["ios"],
                "stack_preferences": ["SwiftUI"],
                "primary_workflows": [],
                "integrations": [],
                "quality_priorities": [],
            },
        },
        archetype={"id": "consumer_ios_app", "label": "Consumer iOS app", "rationale": "iOS signal."},
        bootstrap=_bootstrap(),
        scaffold_spec=[{"name": "ios", "purpose": "iOS app sources", "agent": "mobile"}],
    )

    assert plan["actions"]["materialize"] is True
    assert plan["intent"]["project_type"] == "ios_app"
    assert plan["archetype"]["id"] == "consumer_ios_app"
    assert [m["id"] for m in plan["selected_modules"]] == ["sharedkit"]
    assert "project_type=ios_app" in plan["selected_modules"][0]["matched"]
    assert "platform=ios" in plan["selected_modules"][0]["matched"]
    assert "requires=swiftui" in plan["selected_modules"][0]["matched"]
    assert [m["id"] for m in plan["skipped_modules"]] == ["android-only"]
    assert plan["files_to_write"][0]["path"] == "Package.swift"
    assert "Sources/SharedKit.swift" in {f["path"] for f in plan["files_to_write"]}
    assert plan["context_atoms"] == [
        {"type": "decision", "content": "Use SharedKit for UI primitives.", "module": "sharedkit"}
    ]
    assert plan["pillars"] == [
        {"name": "Native feel", "claim": "Prefer platform conventions.", "module": "sharedkit"}
    ]
    assert {t["summary"] for t in plan["tasks_to_create"]} == {"Add iOS lib", "Wire SharedKit"}


def test_plan_edit_can_remove_and_add_modules():
    mod = _load_module()
    extra_module = {
        "id": "manual-module",
        "name": "Manual module",
        "description": "Added by operator.",
        "files": [{"path": "manual.txt", "content": "manual\n"}],
        "tasks": [
            {
                "summary": "Apply manual module",
                "description": "Apply the manual module.",
                "priority": "Medium",
                "task_type": "feature",
                "complexity": "S",
                "criteria": ["manual module applied"],
            }
        ],
    }

    plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"platforms": ["ios"], "stack_preferences": ["SwiftUI"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap=_bootstrap(),
        remove_modules=["sharedkit"],
        add_modules=[extra_module],
    )

    assert [m["id"] for m in plan["selected_modules"]] == ["manual-module"]
    assert "Sources/SharedKit.swift" not in {f["path"] for f in plan["files_to_write"]}
    assert "manual.txt" in {f["path"] for f in plan["files_to_write"]}
    assert {t["summary"] for t in plan["tasks_to_create"]} == {"Add iOS lib", "Apply manual module"}


def test_plan_action_skip_materialization_removes_file_and_task_actions():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"platforms": ["ios"], "stack_preferences": ["SwiftUI"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap=_bootstrap(),
        scaffold_spec=[{"name": "ios", "purpose": "iOS app sources", "agent": "mobile"}],
        plan_action="skip-materialization",
    )

    assert plan["actions"]["materialize"] is False
    assert plan["scaffold"] == []
    assert plan["files_to_write"] == []
    assert plan["tasks_to_create"] == []


def test_cli_outputs_plan_json(capsys):
    mod = _load_module()
    rc = mod.main(
        [
            "ignored.db",
            "ignored-config.json",
            "--picked",
            json.dumps({"project_type": "ios_app", "init_intent": {"platforms": ["ios"], "stack_preferences": ["SwiftUI"]}}),
            "--archetype",
            json.dumps({"id": "consumer_ios_app"}),
            "--bootstrap",
            json.dumps(_bootstrap()),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["plan"]["selected_modules"][0]["id"] == "sharedkit"


def test_cli_accepts_wizard_style_module_edit_flags(capsys):
    mod = _load_module()
    rc = mod.main(
        [
            "ignored.db",
            "ignored-config.json",
            "--picked",
            json.dumps({"project_type": "ios_app", "init_intent": {"platforms": ["ios"], "stack_preferences": ["SwiftUI"]}}),
            "--archetype",
            json.dumps({"id": "consumer_ios_app"}),
            "--bootstrap",
            json.dumps(_bootstrap()),
            "--plan-remove-module",
            "sharedkit",
            "--plan-add-module",
            json.dumps({
                "id": "manual-module",
                "name": "Manual module",
                "description": "Added by operator.",
            }),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in payload["plan"]["selected_modules"]] == ["manual-module"]
