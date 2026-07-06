from __future__ import annotations

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-apply-memory.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_apply_memory", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan():
    return {
        "intent": {
            "project_type": "python_service",
            "init_intent": {
                "audience": "Partner systems",
                "primary_workflows": ["ingest webhook"],
                "integrations": ["Stripe"],
                "data_needs": ["events"],
                "quality_priorities": ["observability"],
                "non_goals": ["public signup"],
                "open_questions": ["PagerDuty or native schedules?"],
            },
        },
        "archetype": {
            "id": "api_service",
            "label": "API service",
            "rationale": "Backend workflow signal found.",
        },
        "context_atoms": [
            {"type": "decision", "content": "Use structured logging.", "module": "python-logging"},
        ],
        "pillars": [
            {"name": "Operability", "claim": "Production behavior must be observable.", "module": "python-logging"},
        ],
        "glossary": [
            {"term": "Webhook", "definition": "A callback delivered by a partner system.", "module": "api-client"},
        ],
    }


def test_derive_memory_entries_from_plan_and_intent():
    mod = _load_module()

    memory = mod.derive_memory(_plan())

    assert {"type": "decision", "content": "Use structured logging."} in memory["context_atoms"]
    assert {"type": "decision", "content": "Inferred archetype: API service — Backend workflow signal found."} in memory["context_atoms"]
    assert {"type": "memory", "content": "Audience: Partner systems"} in memory["context_atoms"]
    assert {"type": "memory", "content": "Primary workflow: ingest webhook"} in memory["context_atoms"]
    assert {"type": "memory", "content": "Integration: Stripe"} in memory["context_atoms"]
    assert {"type": "memory", "content": "Data need: events"} in memory["context_atoms"]
    assert {"type": "decision", "content": "Quality priority: observability"} in memory["context_atoms"]
    assert {"type": "assumption", "content": "Non-goal: public signup"} in memory["context_atoms"]
    assert {"type": "question", "content": "Open question: PagerDuty or native schedules?"} in memory["context_atoms"]
    assert {"name": "Operability", "claim": "Production behavior must be observable."} in memory["pillars"]
    assert {"name": "Observability", "claim": "The system should make observability a first-class quality priority."} in memory["pillars"]
    assert {"term": "Webhook", "definition": "A callback delivered by a partner system."} in memory["glossary"]


def test_apply_memory_skips_existing_rows():
    mod = _load_module()

    existing = {
        "context_atoms": [
            {"type": "memory", "content": "Audience: Partner systems"},
        ],
        "pillars": [
            {"name": "Operability", "core_claim": "Production behavior must be observable."},
        ],
        "glossary": [
            {"term": "Webhook", "definition": "A callback delivered by a partner system."},
        ],
    }
    memory = mod.derive_memory(_plan())

    result = mod.plan_memory_application(memory, existing)

    assert {"type": "memory", "content": "Audience: Partner systems"} in result["skipped_context_atoms"]
    assert {"name": "Operability", "claim": "Production behavior must be observable."} in result["skipped_pillars"]
    assert {"term": "Webhook", "definition": "A callback delivered by a partner system."} in result["skipped_glossary"]
    assert {"type": "question", "content": "Open question: PagerDuty or native schedules?"} in result["context_atoms_to_add"]
