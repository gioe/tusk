#!/usr/bin/env python3
"""Normalize project intent captured during tusk-init.

The intent record is the durable contract between the fresh-project interview
and later bootstrap planning steps. It intentionally stays small and
configuration-shaped so it can be persisted under tusk/config.json:init_intent
without introducing new schema tables before the richer planner exists.

Usage:
    tusk init-intent questions
    tusk init-intent follow-ups --answers '<json object>'
    tusk init-intent archetype --answers '<json object>' [--scan '<json object>'] [--override <id>]
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

ARCHETYPE_DEFAULTS = {
    "consumer_ios_app": {
        "label": "Consumer iOS app",
        "project_type": "ios_app",
        "domains": ["mobile"],
        "agents": ["mobile", "general"],
        "pillar_hints": ["polished UX", "privacy", "offline resilience"],
        "utility_modules": ["ios_app"],
        "first_slice_tasks": ["Build the first mobile workflow end to end"],
    },
    "internal_tool": {
        "label": "Internal tool",
        "project_type": "web_app",
        "domains": ["frontend"],
        "agents": ["frontend", "general"],
        "pillar_hints": ["operator efficiency", "auditability"],
        "utility_modules": ["web_app"],
        "first_slice_tasks": ["Build the first operator workflow end to end"],
    },
    "b2b_dashboard": {
        "label": "B2B dashboard",
        "project_type": "web_app",
        "domains": ["frontend", "api", "database"],
        "agents": ["frontend", "backend", "general"],
        "pillar_hints": ["account workflow clarity", "auditability", "integration reliability"],
        "utility_modules": ["web_app", "backend"],
        "first_slice_tasks": ["Build the first account workflow end to end"],
    },
    "api_service": {
        "label": "API service",
        "project_type": "python_service",
        "domains": ["api", "database"],
        "agents": ["backend", "general"],
        "pillar_hints": ["contract clarity", "observability", "reliability"],
        "utility_modules": ["python_service"],
        "first_slice_tasks": ["Build the first API endpoint with persistence"],
    },
    "content_site": {
        "label": "Content site",
        "project_type": "docs_site",
        "domains": ["frontend", "docs"],
        "agents": ["frontend", "docs", "general"],
        "pillar_hints": ["editorial clarity", "discoverability"],
        "utility_modules": ["web_app"],
        "first_slice_tasks": ["Publish the first content route"],
    },
    "library": {
        "label": "Library/package",
        "project_type": "library",
        "domains": ["api", "docs"],
        "agents": ["backend", "docs", "general"],
        "pillar_hints": ["small public API", "documentation quality"],
        "utility_modules": [],
        "first_slice_tasks": ["Ship the first importable module with docs"],
    },
    "data_pipeline": {
        "label": "Data pipeline",
        "project_type": "data_pipeline",
        "domains": ["data", "infrastructure"],
        "agents": ["data", "infrastructure", "general"],
        "pillar_hints": ["data correctness", "observability"],
        "utility_modules": ["backend"],
        "first_slice_tasks": ["Run the first ingest-transform-output path"],
    },
    "monorepo": {
        "label": "Monorepo",
        "project_type": "monorepo",
        "domains": [],
        "agents": ["general"],
        "pillar_hints": ["clear package ownership", "fast local workflows"],
        "utility_modules": [],
        "first_slice_tasks": ["Create the first package-level vertical slice"],
    },
    "ambiguous": {
        "label": "Ambiguous project",
        "project_type": None,
        "domains": [],
        "agents": ["general"],
        "pillar_hints": [],
        "utility_modules": [],
        "first_slice_tasks": [],
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


def _scan_domains(scan: dict[str, Any] | None) -> list[str]:
    if not isinstance(scan, dict):
        return []
    domains: list[str] = []
    seen: set[str] = set()
    for item in scan.get("detected_domains") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_scalar(item.get("name"))
        if name and name not in seen:
            seen.add(name)
            domains.append(name)
    return domains


def _scan_manifests(scan: dict[str, Any] | None) -> list[str]:
    if not isinstance(scan, dict):
        return []
    return [str(item) for item in scan.get("manifests") or []]


def _text_blob(intent: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in INTENT_FIELDS:
        value = intent.get(field)
        if isinstance(value, list):
            parts.extend(value)
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _merge_domains(defaults: list[str], scan_domains: list[str]) -> list[str]:
    source = scan_domains or defaults
    merged: list[str] = []
    for domain in source:
        if domain not in merged:
            merged.append(domain)
    return merged


def _archetype_payload(archetype_id: str, rationale: list[str], scan_domains: list[str]) -> dict[str, Any]:
    defaults = ARCHETYPE_DEFAULTS[archetype_id]
    domains = _merge_domains(list(defaults["domains"]), scan_domains)
    return {
        "id": archetype_id,
        "label": defaults["label"],
        "project_type": defaults["project_type"],
        "domains": domains,
        "agents": list(defaults["agents"]),
        "pillar_hints": list(defaults["pillar_hints"]),
        "utility_modules": list(defaults["utility_modules"]),
        "first_slice_tasks": list(defaults["first_slice_tasks"]),
        "rationale": "; ".join(rationale) if rationale else "No strong intent or codebase signal yet.",
    }


def infer_archetype(
    raw: dict[str, Any],
    *,
    scan: dict[str, Any] | None = None,
    override: str | None = None,
) -> dict[str, Any]:
    intent = normalize_intent(raw)
    scan_domains = _scan_domains(scan)
    manifests = _scan_manifests(scan)
    text = _text_blob(intent)
    platforms = set(intent["platforms"])
    project_type = intent.get("project_type")

    if override:
        if override not in ARCHETYPE_DEFAULTS:
            raise ValueError(f"unknown archetype override: {override}")
        return _archetype_payload(override, [f"User selected {ARCHETYPE_DEFAULTS[override]['label']}"], scan_domains)

    if (
        project_type == "monorepo"
        or "monorepo" in text
        or "turborepo" in text
        or any(path.startswith(("apps/", "packages/")) for path in manifests)
    ):
        return _archetype_payload("monorepo", ["Monorepo/package layout signal found"], scan_domains)

    if project_type == "ios_app" or "ios" in platforms or "swiftui" in text:
        return _archetype_payload("consumer_ios_app", ["iOS/mobile launch surface found"], scan_domains)

    if (
        project_type == "python_service"
        or "backend" in platforms
        or "api" in text
        or "webhook" in text
        or scan_domains == ["api"]
    ):
        return _archetype_payload("api_service", ["Backend/API workflow signal found"], scan_domains)

    if (
        "data" in platforms
        or "pipeline" in text
        or "ml" in text
        or "pandas" in text
        or "data" in scan_domains
    ):
        return _archetype_payload("data_pipeline", ["Data or pipeline signal found"], scan_domains)

    if "docs" in platforms or "documentation" in text or "content" in text or "docs" in scan_domains:
        return _archetype_payload("content_site", ["Content/documentation signal found"], scan_domains)

    if "library" in text or "package" in text:
        return _archetype_payload("library", ["Library/package signal found"], scan_domains)

    if "web" in platforms or "frontend" in scan_domains:
        if (
            "b2b" in text
            or "customer" in text
            or "account" in text
            or "salesforce" in text
            or {"api", "database"}.issubset(set(scan_domains))
        ):
            rationale = ["B2B/dashboard workflow and integration signal found"]
            integrations = intent.get("integrations") or []
            if integrations:
                rationale.append(f"Integrations: {', '.join(integrations)}")
            return _archetype_payload("b2b_dashboard", rationale, scan_domains)
        if "internal" in text or "operations" in text or "operator" in text:
            return _archetype_payload("internal_tool", ["Internal/operator workflow signal found"], scan_domains)
        return _archetype_payload("internal_tool", ["Web workflow signal found"], scan_domains)

    return _archetype_payload("ambiguous", [], scan_domains)


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


def cmd_archetype(args: argparse.Namespace) -> int:
    try:
        answers = json.loads(args.answers)
        scan = json.loads(args.scan) if args.scan is not None else None
        intent = normalize_intent(answers)
        archetype = infer_archetype(intent, scan=scan, override=args.override)
    except (json.JSONDecodeError, ValueError) as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, "intent": intent, "archetype": archetype}))
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

    archetype = sub.add_parser(
        "archetype",
        allow_abbrev=False,
        help="Infer a project archetype from normalized intent and optional scan output.",
    )
    archetype.add_argument("--answers", required=True, help="JSON object of raw answers.")
    archetype.add_argument("--scan", default=None, help="Optional init-scan-codebase JSON output.")
    archetype.add_argument("--override", default=None, help="User-selected archetype id.")

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
    if args.mode == "archetype":
        return cmd_archetype(args)
    if args.mode == "normalize":
        return cmd_normalize(args)
    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-intent normalize --answers '<json>'", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
