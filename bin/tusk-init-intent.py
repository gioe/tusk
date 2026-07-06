#!/usr/bin/env python3
"""Normalize project intent captured during tusk-init.

The intent record is the durable contract between the fresh-project interview
and later bootstrap planning steps. It intentionally stays small and
configuration-shaped so it can be persisted under tusk/config.json:init_intent
without introducing new schema tables before the richer planner exists.

Usage:
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


def normalize_intent(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("intent answers must be a JSON object")

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

    normalize = sub.add_parser(
        "normalize",
        allow_abbrev=False,
        help="Normalize raw intent answers into the stable init_intent contract.",
    )
    normalize.add_argument("--answers", required=True, help="JSON object of raw answers.")

    args = parser.parse_args(argv[2:])
    if args.mode == "normalize":
        return cmd_normalize(args)
    return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-intent normalize --answers '<json>'", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
