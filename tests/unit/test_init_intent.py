from __future__ import annotations

import importlib.util
import json
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-intent.py")
WIZARD_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-wizard.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_intent", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_wizard_module():
    spec = importlib.util.spec_from_file_location("tusk_init_wizard", WIZARD_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_normalize_intent_preserves_stable_fields_and_list_values():
    mod = _load_module()

    intent = mod.normalize_intent(
        {
            "audience": "Independent musicians",
            "primary_workflows": "import tracks, review royalty splits",
            "platforms": ["iOS", "Web"],
            "stack_preferences": "SwiftUI, Next.js",
            "integrations": "Stripe, Spotify",
            "data_needs": ["tracks", "royalty splits"],
            "quality_priorities": "offline support, privacy",
            "launch_target": "private beta in September",
            "non_goals": "social feed",
            "open_questions": "which royalty provider?",
            "project_type": "ios_app",
        }
    )

    assert intent == {
        "audience": "Independent musicians",
        "primary_workflows": ["import tracks", "review royalty splits"],
        "platforms": ["ios", "web"],
        "stack_preferences": ["SwiftUI", "Next.js"],
        "integrations": ["Stripe", "Spotify"],
        "data_needs": ["tracks", "royalty splits"],
        "quality_priorities": ["offline support", "privacy"],
        "launch_target": "private beta in September",
        "non_goals": ["social feed"],
        "open_questions": ["which royalty provider?"],
        "project_type": "ios_app",
    }


def test_normalize_intent_fills_missing_fields_without_clobbering_project_type():
    mod = _load_module()

    intent = mod.normalize_intent({"project_type": "python_service"})

    assert set(intent) == set(mod.INTENT_FIELDS)
    assert intent["project_type"] == "python_service"
    for key in mod.LIST_FIELDS:
        assert intent[key] == []
    assert intent["audience"] is None
    assert intent["launch_target"] is None


def test_cli_normalize_outputs_compact_json(capsys):
    mod = _load_module()
    rc = mod.main([
        "ignored.db",
        "ignored-config.json",
        "normalize",
        "--answers",
        json.dumps({"platforms": "Android, backend", "project_type": "android_app"}),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["intent"]["platforms"] == ["android", "backend"]
    assert payload["intent"]["project_type"] == "android_app"


def test_interview_questions_collect_intent_before_routing_decisions():
    mod = _load_module()

    questions = mod.interview_questions()

    assert [q["id"] for q in questions[:6]] == [
        "audience",
        "primary_workflows",
        "platforms",
        "stack_preferences",
        "integrations",
        "quality_priorities",
    ]
    assert all(q["phase"] == "intent" for q in questions)
    assert "domains" not in {q["target_field"] for q in questions}
    assert "agents" not in {q["target_field"] for q in questions}
    assert "scaffold" not in {q["target_field"] for q in questions}


def test_mobile_app_without_platform_gets_one_conditional_followup():
    mod = _load_module()

    questions = mod.follow_up_questions(
        {
            "audience": "Parents coordinating youth sports",
            "primary_workflows": "create team schedule, message families",
            "platforms": "mobile app",
            "stack_preferences": "",
        }
    )

    assert [q["id"] for q in questions] == ["mobile_platform"]


def test_ios_field_work_scenario_feeds_the_intent_model():
    mod = _load_module()

    intent = mod.normalize_intent(
        {
            "who_it_serves": "Field technicians",
            "first_workflows": "capture inspection, sync report",
            "platforms": "iPhone",
            "planned_stack": "SwiftUI",
            "expected_integrations": "company SSO",
            "data_model_hints": "inspections, photos",
            "quality_priorities": "offline support, privacy",
            "launch_constraints": "pilot next quarter",
            "project_type": "ios_app",
        }
    )

    assert intent["audience"] == "Field technicians"
    assert intent["primary_workflows"] == ["capture inspection", "sync report"]
    assert intent["platforms"] == ["ios"]
    assert intent["stack_preferences"] == ["SwiftUI"]
    assert intent["integrations"] == ["company SSO"]
    assert intent["data_needs"] == ["inspections", "photos"]
    assert intent["quality_priorities"] == ["offline support", "privacy"]
    assert intent["launch_target"] == "pilot next quarter"
    assert intent["project_type"] == "ios_app"


def test_internal_web_dashboard_scenario_does_not_get_mobile_followup():
    mod = _load_module()

    answers = {
        "audience": "Operations managers",
        "primary_workflows": "review queue, approve exceptions",
        "platforms": "web",
        "stack_preferences": "Next.js, Prisma",
        "integrations": "Slack, Okta",
        "data_needs": "queues, approvals",
        "quality_priorities": "auditability, fast filters",
        "project_type": "web_app",
    }

    assert mod.follow_up_questions(answers) == []
    intent = mod.normalize_intent(answers)
    assert intent["platforms"] == ["web"]
    assert intent["integrations"] == ["Slack", "Okta"]
    assert intent["data_needs"] == ["queues", "approvals"]


def test_backend_service_scenario_prompts_for_missing_data_model_only():
    mod = _load_module()

    answers = {
        "audience": "Partner systems",
        "primary_workflows": "ingest webhook, expose reporting API",
        "platforms": "backend",
        "integrations": "Stripe",
        "project_type": "python_service",
    }

    questions = mod.follow_up_questions(answers)
    assert [q["id"] for q in questions] == ["data_needs"]


def test_fresh_project_wizard_collects_intent_before_domain_prompts(monkeypatch):
    wizard = _load_wizard_module()
    prompts = []
    answers = iter(
        [
            "Field technicians",
            "capture inspection, sync report",
            "ios",
            "SwiftUI",
            "",
            "offline support",
            "",
            "",
            "",
            "bug,feature",
            "pytest",
            "ios_app",
        ]
    )

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)

    picked = wizard._interactive_collect(
        {"manifests": [], "detected_domains": []},
        {"command": "pytest"},
        {},
    )

    assert prompts[0].startswith("Who is this project for")
    assert prompts[6].startswith("Archetype")
    assert prompts[7].startswith("Domains ")
    assert picked["init_intent"]["audience"] == "Field technicians"
    assert picked["init_intent"]["platforms"] == ["ios"]


def test_infer_archetype_uses_mobile_intent_for_consumer_ios_app():
    mod = _load_module()

    result = mod.infer_archetype(
        {
            "audience": "Parents coordinating youth sports",
            "primary_workflows": "create schedule, message team",
            "platforms": "ios",
            "stack_preferences": "SwiftUI",
            "quality_priorities": "offline support, polished UX",
        }
    )

    assert result["id"] == "consumer_ios_app"
    assert result["project_type"] == "ios_app"
    assert result["domains"] == ["mobile"]
    assert "mobile" in result["agents"]
    assert "iOS" in result["rationale"]


def test_infer_archetype_uses_web_intent_and_scan_for_b2b_dashboard():
    mod = _load_module()

    result = mod.infer_archetype(
        {
            "audience": "Customer success teams at B2B SaaS companies",
            "primary_workflows": "review accounts, approve renewals",
            "platforms": "web",
            "stack_preferences": "Next.js, Prisma",
            "integrations": "Salesforce, Okta",
            "data_needs": "accounts, renewals",
        },
        scan={
            "detected_domains": [
                {"name": "frontend", "confidence": "high", "signals": ["app/"]},
                {"name": "api", "confidence": "high", "signals": ["api/"]},
                {"name": "database", "confidence": "high", "signals": ["prisma/"]},
            ]
        },
    )

    assert result["id"] == "b2b_dashboard"
    assert result["domains"] == ["frontend", "api", "database"]
    assert result["project_type"] == "web_app"
    assert "Salesforce" in result["rationale"]


def test_infer_archetype_uses_backend_intent_for_api_service():
    mod = _load_module()

    result = mod.infer_archetype(
        {
            "audience": "Partner systems",
            "primary_workflows": "ingest webhook, expose reporting API",
            "platforms": "backend",
            "integrations": "Stripe",
            "data_needs": "events, reports",
            "project_type": "python_service",
        }
    )

    assert result["id"] == "api_service"
    assert result["project_type"] == "python_service"
    assert result["domains"] == ["api", "database"]


def test_infer_archetype_uses_scan_for_monorepo():
    mod = _load_module()

    result = mod.infer_archetype(
        {
            "audience": "Internal platform team",
            "primary_workflows": "ship web app and shared packages",
            "platforms": "web, backend",
            "stack_preferences": "Turborepo, Next.js",
        },
        scan={
            "manifests": ["apps/web/package.json", "packages/ui/package.json"],
            "detected_domains": [
                {"name": "frontend", "confidence": "high", "signals": ["apps/web"]},
                {"name": "api", "confidence": "medium", "signals": ["apps/api"]},
            ],
        },
    )

    assert result["id"] == "monorepo"
    assert result["project_type"] == "monorepo"
    assert result["domains"] == ["frontend", "api"]


def test_infer_archetype_returns_ambiguous_when_signals_are_weak():
    mod = _load_module()

    result = mod.infer_archetype({"audience": "A small team"})

    assert result["id"] == "ambiguous"
    assert result["project_type"] is None
    assert result["domains"] == []


def test_cli_archetype_supports_user_override_without_changing_intent(capsys):
    mod = _load_module()
    answers = {
        "audience": "Operations team",
        "primary_workflows": "review queue",
        "platforms": "web",
    }

    rc = mod.main([
        "ignored.db",
        "ignored-config.json",
        "archetype",
        "--answers",
        json.dumps(answers),
        "--override",
        "internal_tool",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["archetype"]["id"] == "internal_tool"
    assert payload["intent"]["platforms"] == ["web"]


def test_fresh_project_wizard_uses_archetype_defaults_and_allows_override(monkeypatch):
    wizard = _load_wizard_module()
    prompts = []
    answers = iter(
        [
            "Operations team",
            "review queue, approve exceptions",
            "web",
            "Next.js",
            "Okta",
            "auditability",
            "internal_tool",
            "",
            "",
            "bug,feature",
            "pytest",
            "",
        ]
    )

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)

    picked = wizard._interactive_collect(
        {"manifests": [], "detected_domains": []},
        {"command": "pytest"},
        {},
    )

    assert any(prompt.startswith("Archetype") for prompt in prompts)
    assert picked["init_intent"]["audience"] == "Operations team"
    assert picked["project_type"] == "web_app"
    assert picked["domains"] == ["frontend"]
    assert set(picked["agents"]) == {"frontend", "general"}
