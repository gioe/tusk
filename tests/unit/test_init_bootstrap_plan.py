from __future__ import annotations

import importlib.util
import json
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-bootstrap-plan.py")
IOS_BOOTSTRAP_EXAMPLE = os.path.join(REPO_ROOT, "docs", "bootstrap-packs", "ios-libs", "tusk-bootstrap.json")


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


def _ios_bootstrap_example():
    with open(IOS_BOOTSTRAP_EXAMPLE, "r", encoding="utf-8") as f:
        return {"libs": [json.load(f) | {"name": "ios_app", "repo": "gioe/ios-libs", "error": None}]}


def test_plan_selects_applicable_modules_and_explains_reasons():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "platforms": ["ios"],
                "stack_preferences": ["SwiftUI", "mobile"],
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
    task_summaries = {t["summary"] for t in plan["tasks_to_create"]}
    assert {"Add iOS lib", "Wire SharedKit"}.issubset(task_summaries)
    assert any(t["source"].startswith("vertical_slice:") for t in plan["tasks_to_create"])


def test_plan_selects_real_ios_bootstrap_pack_modules_from_intent():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "audience": "Field inspectors",
                "primary_workflows": ["capture inspection"],
                "platforms": ["ios"],
                "stack_preferences": ["SwiftUI", "mobile"],
                "integrations": ["api"],
                "data_needs": ["inspections"],
                "quality_priorities": ["observability", "offline"],
            },
        },
        archetype={"id": "consumer_ios_app", "label": "Consumer iOS app", "rationale": "iOS signal."},
        bootstrap=_ios_bootstrap_example(),
    )

    selected_ids = {module["id"] for module in plan["selected_modules"]}
    assert {
        "sharedkit-design-system",
        "api-client",
        "navigation-shell",
        "observability",
    } <= selected_ids
    assert "persistence" in selected_ids
    assert any(item["mode"] == "marker_block" for item in plan["files_to_write"])
    assert any(item["type"] == "decision" and "SharedKit" in item["content"] for item in plan["context_atoms"])
    assert any(pillar["name"] == "Native feel" for pillar in plan["pillars"])
    assert any(task["summary"] == "Wire SharedKit design tokens" for task in plan["tasks_to_create"])
    assert any(task["source"].startswith("vertical_slice:") for task in plan["tasks_to_create"])


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
    task_summaries = {t["summary"] for t in plan["tasks_to_create"]}
    assert {"Add iOS lib", "Apply manual module"}.issubset(task_summaries)
    assert any(t["source"].startswith("vertical_slice:") for t in plan["tasks_to_create"])


def test_plan_includes_vertical_slice_tasks_from_intent():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "primary_workflows": ["capture inspection"],
                "platforms": ["ios"],
                "data_needs": ["inspections"],
                "integrations": ["weather api"],
                "quality_priorities": ["offline support"],
            },
        },
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
    )

    task = next(t for t in plan["tasks_to_create"] if t["source"].startswith("vertical_slice:"))
    assert task["id"] == "vertical-slice-mobile-capture-inspection"
    assert "capture inspection" in task["summary"].lower()
    assert any("test" in c.lower() for c in task["criteria"])


def test_plan_task_controls_can_pick_remove_skip_and_add_tasks():
    mod = _load_module()
    manual = {
        "id": "vertical-slice-manual",
        "summary": "Build edited starter slice",
        "description": "Edited by operator.",
        "priority": "High",
        "task_type": "feature",
        "complexity": "S",
        "criteria": ["Edited behavior is verified."],
    }

    picked_plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
        task_mode="pick",
        task_ids=["vertical-slice-mobile-capture-inspection"],
    )
    assert [t["id"] for t in picked_plan["tasks_to_create"]] == ["vertical-slice-mobile-capture-inspection"]

    removed_plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
        remove_tasks=["vertical-slice-mobile-capture-inspection"],
        add_tasks=[manual],
    )
    assert [t["id"] for t in removed_plan["tasks_to_create"]] == ["vertical-slice-manual"]

    skipped_plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
        task_mode="none",
    )
    assert skipped_plan["tasks_to_create"] == []


def test_plan_task_pick_preserves_idless_bootstrap_tasks():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "platforms": ["ios"],
                "stack_preferences": ["SwiftUI"],
                "primary_workflows": ["capture inspection"],
            },
        },
        archetype={"id": "consumer_ios_app"},
        bootstrap=_bootstrap(),
        task_mode="pick",
        task_ids=["vertical-slice-mobile-capture-inspection"],
    )

    assert {t["summary"] for t in plan["tasks_to_create"]} == {
        "Add iOS lib",
        "Wire SharedKit",
        "Ship first mobile vertical slice for capture inspection",
    }


def test_plan_task_none_preserves_idless_bootstrap_tasks():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "platforms": ["ios"],
                "stack_preferences": ["SwiftUI"],
                "primary_workflows": ["capture inspection"],
            },
        },
        archetype={"id": "consumer_ios_app"},
        bootstrap=_bootstrap(),
        task_mode="none",
    )

    assert {t["summary"] for t in plan["tasks_to_create"]} == {"Add iOS lib", "Wire SharedKit"}


def test_plan_task_controls_reject_unknown_task_ids():
    mod = _load_module()

    for kwargs in (
        {"task_mode": "pick", "task_ids": ["missing-task"]},
        {"remove_tasks": ["missing-task"]},
    ):
        try:
            mod.build_bootstrap_plan(
                picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
                archetype={"id": "consumer_ios_app"},
                bootstrap={"libs": []},
                **kwargs,
            )
        except ValueError as exc:
            assert "unknown task id" in str(exc).lower()
            assert "missing-task" in str(exc)
        else:
            raise AssertionError("expected ValueError")


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
