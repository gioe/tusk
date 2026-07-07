from __future__ import annotations

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-vertical-slice.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_vertical_slice", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _criterion_text(task):
    return " ".join(task["criteria"]).lower()


def test_mobile_proposal_uses_workflow_data_verification_and_docs():
    mod = _load_module()

    tasks = mod.generate_vertical_slice_tasks(
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
        selected_modules=[{"id": "sharedkit", "name": "SharedKit"}],
    )

    assert len(tasks) == 1
    task = tasks[0]
    assert task["id"] == "vertical-slice-mobile-capture-inspection"
    assert "capture inspection" in task["summary"].lower()
    assert task["criteria"][2] == (
        "Connect the weather api integration through the SharedKit module boundary "
        "and document any stubbed or deferred behavior."
    )
    criteria = _criterion_text(task)
    assert "screen" in criteria or "ui" in criteria
    assert "inspections" in criteria
    assert "weather api" in criteria
    assert "test" in criteria
    assert "document" in criteria


def test_web_proposal_uses_route_state_integration_verification_and_docs():
    mod = _load_module()

    tasks = mod.generate_vertical_slice_tasks(
        picked={
            "project_type": "web_app",
            "init_intent": {
                "primary_workflows": ["review customer queue"],
                "platforms": ["web"],
                "data_needs": ["customers"],
                "integrations": ["stripe"],
                "quality_priorities": ["keyboard workflows"],
            },
        },
        archetype={"id": "internal_dashboard"},
        selected_modules=[],
    )

    task = tasks[0]
    assert task["id"] == "vertical-slice-web-review-customer-queue"
    criteria = _criterion_text(task)
    assert "route" in criteria or "page" in criteria
    assert "customers" in criteria
    assert "stripe" in criteria
    assert "test" in criteria
    assert "document" in criteria


def test_backend_proposal_uses_endpoint_schema_integration_verification_and_docs():
    mod = _load_module()

    tasks = mod.generate_vertical_slice_tasks(
        picked={
            "project_type": "python_service",
            "init_intent": {
                "primary_workflows": ["submit intake request"],
                "platforms": ["api"],
                "data_needs": ["intake requests"],
                "integrations": ["postgres"],
                "quality_priorities": ["audit trail"],
            },
        },
        archetype={"id": "api_service"},
        selected_modules=[{"id": "structured-logging", "name": "Structured logging"}],
    )

    task = tasks[0]
    assert task["id"] == "vertical-slice-backend-submit-intake-request"
    criteria = _criterion_text(task)
    assert "endpoint" in criteria or "service" in criteria
    assert "intake requests" in criteria
    assert "postgres" in criteria
    assert "test" in criteria
    assert "document" in criteria


def test_family_detection_uses_platform_aliases_and_archetype_text():
    mod = _load_module()

    cases = [
        (
            {"platform": "ios"},
            None,
            "vertical-slice-mobile-",
        ),
        (
            {"project_type": "other", "init_intent": {"platform": "api"}},
            None,
            "vertical-slice-backend-",
        ),
        (
            {"init_intent": {}},
            {"name": "mobile app"},
            "vertical-slice-mobile-",
        ),
    ]

    for picked, archetype, expected_prefix in cases:
        task = mod.generate_vertical_slice_tasks(picked=picked, archetype=archetype)[0]

        assert task["id"].startswith(expected_prefix)


def test_selected_module_boundary_does_not_duplicate_integration_word():
    mod = _load_module()

    task = mod.generate_vertical_slice_tasks(
        picked={
            "project_type": "web_app",
            "init_intent": {
                "integrations": ["Stripe integration API"],
            },
        },
        selected_modules=[{"id": "payments", "name": "Payments"}],
    )[0]

    assert task["criteria"][2] == (
        "Connect the Stripe integration API through the Payments module boundary "
        "and document any stubbed or deferred behavior."
    )
    assert "integration API integration" not in task["criteria"][2]


def test_family_detection_covers_all_declared_aliases():
    mod = _load_module()

    cases = [
        ("ios", "mobile"),
        ("android", "mobile"),
        ("mobile", "mobile"),
        ("api", "backend"),
        ("backend", "backend"),
        ("service", "backend"),
        ("python", "backend"),
    ]

    for signal, expected_family in cases:
        task = mod.generate_vertical_slice_tasks(picked={"platform": signal})[0]

        assert task["id"].startswith(f"vertical-slice-{expected_family}-")
