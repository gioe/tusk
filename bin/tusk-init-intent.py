#!/usr/bin/env python3
"""Normalize project intent captured during tusk-init.

The intent record is the durable contract between the fresh-project interview
and later bootstrap planning steps. It intentionally stays small and
configuration-shaped so it can be persisted under tusk/config.json:init_intent
without introducing new schema tables before the richer planner exists.

Usage:
    tusk init-intent questions
    tusk init-intent follow-ups --answers '<json object>'
    tusk init-intent normalize --answers '<json object>'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps


LIST_FIELDS = (
    "primary_workflows",
    "platforms",
    "stack_preferences",
    "integrations",
    "data_needs",
    "quality_priorities",
    "non_goals",
    "open_questions",
)

SCALAR_FIELDS = (
    "audience",
    "launch_target",
    "project_type",
)

INTENT_FIELDS = (
    "audience",
    "primary_workflows",
    "platforms",
    "stack_preferences",
    "integrations",
    "data_needs",
    "quality_priorities",
    "launch_target",
    "non_goals",
    "open_questions",
    "project_type",
)

FIELD_ALIASES = {
    "who_it_serves": "audience",
    "first_workflows": "primary_workflows",
    "planned_stack": "stack_preferences",
    "expected_integrations": "integrations",
    "data_model_hints": "data_needs",
    "launch_constraints": "launch_target",
}

PLATFORM_ALIASES = {
    "ios": "ios",
    "iphone": "ios",
    "ipad": "ios",
    "android": "android",
    "web": "web",
    "browser": "web",
    "backend": "backend",
    "api": "backend",
    "cli": "cli",
    "macos": "macos",
}

INTERVIEW_QUESTIONS = (
    {
        "id": "audience",
        "prompt": "Who is this project for, and what problem should it solve?",
        "target_field": "audience",
        "kind": "text",
        "phase": "intent",
    },
    {
        "id": "primary_workflows",
        "prompt": "What are the first user or system workflows that should work end to end?",
        "target_field": "primary_workflows",
        "kind": "list",
        "phase": "intent",
    },
    {
        "id": "platforms",
        "prompt": "Which platforms or surfaces matter at launch?",
        "target_field": "platforms",
        "kind": "list",
        "phase": "intent",
    },
    {
        "id": "stack_preferences",
        "prompt": "What languages, frameworks, or architecture preferences should shape the bootstrap?",
        "target_field": "stack_preferences",
        "kind": "list",
        "phase": "intent",
    },
    {
        "id": "integrations",
        "prompt": "Which external systems, auth providers, APIs, or services should it integrate with?",
        "target_field": "integrations",
        "kind": "list",
        "phase": "intent",
    },
    {
        "id": "quality_priorities",
        "prompt": "Which quality priorities should influence the upfront software?",
        "target_field": "quality_priorities",
        "kind": "list",
        "phase": "intent",
    },
)

FOLLOW_UPS = {
    "mobile_platform": {
        "id": "mobile_platform",
        "prompt": "Which mobile platform should the bootstrap target: iOS, Android, or cross-platform?",
        "target_field": "platforms",
        "kind": "choice",
        "phase": "intent",
    },
    "data_needs": {
        "id": "data_needs",
        "prompt": "What are the core data objects, records, or events the first version needs?",
        "target_field": "data_needs",
        "kind": "list",
        "phase": "intent",
    },
}


def _clean_scalar(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_list(value: Any, *, normalize_platforms: bool = False) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]

    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = str(raw).strip()
        if not text:
            continue
        if normalize_platforms:
            text = PLATFORM_ALIASES.get(text.lower(), text.lower())
        if text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _with_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    expanded = dict(raw)
    for source, target in FIELD_ALIASES.items():
        if target not in expanded and source in raw:
            expanded[target] = raw[source]
    return expanded


def interview_questions() -> list[dict[str, str]]:
    return [dict(question) for question in INTERVIEW_QUESTIONS]


def follow_up_questions(raw: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        raise ValueError("intent answers must be a JSON object")

    answers = _with_aliases(raw)
    normalized_platforms = _split_list(answers.get("platforms"), normalize_platforms=True)
    raw_platforms = " ".join(str(item).lower() for item in _split_list(answers.get("platforms")))
    stack = " ".join(str(item).lower() for item in _split_list(answers.get("stack_preferences")))
    workflows = " ".join(str(item).lower() for item in _split_list(answers.get("primary_workflows")))
    project_type = str(answers.get("project_type") or "").lower()

    questions: list[dict[str, str]] = []
    mobile_signal = (
        "mobile" in raw_platforms
        or "mobile" in workflows
        or "mobile" in stack
        or project_type in ("mobile_app", "mobile_cross_platform")
    )
    concrete_mobile_platform = any(
        platform in normalized_platforms for platform in ("ios", "android")
    ) or "react native" in stack or "flutter" in stack
    if mobile_signal and not concrete_mobile_platform:
        questions.append(dict(FOLLOW_UPS["mobile_platform"]))

    backend_signal = (
        "backend" in normalized_platforms
        or "api" in raw_platforms
        or "api" in workflows
        or "webhook" in workflows
        or project_type == "python_service"
    )
    if backend_signal and not _split_list(answers.get("data_needs")):
        questions.append(dict(FOLLOW_UPS["data_needs"]))

    return questions


def normalize_intent(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("intent answers must be a JSON object")

    raw = _with_aliases(raw)
    normalized: dict[str, Any] = {}
    for field in INTENT_FIELDS:
        if field in LIST_FIELDS:
            normalized[field] = _split_list(
                raw.get(field),
                normalize_platforms=(field == "platforms"),
            )
        else:
            normalized[field] = _clean_scalar(raw.get(field))
    return normalized


def cmd_questions(args: argparse.Namespace) -> int:
    print(dumps({"success": True, "questions": interview_questions()}))
    return 0


def cmd_follow_ups(args: argparse.Namespace) -> int:
    try:
        answers = json.loads(args.answers)
        questions = follow_up_questions(answers)
    except (json.JSONDecodeError, ValueError) as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, "questions": questions}))
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    try:
        answers = json.loads(args.answers)
        intent = normalize_intent(answers)
    except (json.JSONDecodeError, ValueError) as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, "intent": intent}))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk init-intent",
        description="Normalize project-intent answers for tusk-init.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    sub.add_parser(
        "questions",
        allow_abbrev=False,
        help="Emit the intent-first fresh-project interview questions.",
    )

    follow_ups = sub.add_parser(
        "follow-ups",
        allow_abbrev=False,
        help="Emit conditional follow-up questions for partial intent answers.",
    )
    follow_ups.add_argument("--answers", required=True, help="JSON object of raw answers.")

    normalize = sub.add_parser(
        "normalize",
        allow_abbrev=False,
        help="Normalize raw intent answers into the stable init_intent contract.",
    )
    normalize.add_argument("--answers", required=True, help="JSON object of raw answers.")

    args = parser.parse_args(argv[2:])
    if args.mode == "questions":
        return cmd_questions(args)
    if args.mode == "follow-ups":
        return cmd_follow_ups(args)
    if args.mode == "normalize":
        return cmd_normalize(args)
    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-intent normalize --answers '<json>'", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
