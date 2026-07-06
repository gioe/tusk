from __future__ import annotations

import importlib.util
import json
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-intent.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_intent", SCRIPT)
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
