#!/usr/bin/env python3
"""Generate first vertical-slice task proposals from tusk-init intent."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps


MOBILE_SIGNALS = ("ios", "android", "mobile")
BACKEND_SIGNALS = ("api", "backend", "service", "python")


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]

    items: list[str] = []
    for raw in raw_items:
        text = str(raw).strip()
        if text:
            items.append(text)
    return items


def _first(value: Any, fallback: str) -> str:
    items = _list(value)
    return items[0] if items else fallback


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "first-workflow"


def _signal_text(
    *,
    picked: dict[str, Any],
    archetype: dict[str, Any] | None,
    intent: dict[str, Any],
) -> str:
    archetype = archetype or {}
    signals = (
        _list(picked.get("project_type") or intent.get("project_type"))
        + _list(archetype.get("id") or archetype.get("archetype"))
        + _list(archetype.get("name"))
        + _list(archetype.get("label"))
        + _list(picked.get("platform"))
        + _list(intent.get("platforms"))
        + _list(intent.get("platform"))
    )
    return " ".join(signals).lower()


def _family(
    *,
    picked: dict[str, Any],
    archetype: dict[str, Any] | None,
    intent: dict[str, Any],
) -> str:
    signals = _signal_text(picked=picked, archetype=archetype, intent=intent)
    if any(signal in signals for signal in MOBILE_SIGNALS):
        return "mobile"
    if any(signal in signals for signal in BACKEND_SIGNALS):
        return "backend"
    return "web"


def _integration_label(integration: str) -> str:
    if re.search(r"\bintegration\b", integration, re.IGNORECASE):
        return integration
    return f"{integration} integration"


def _module_boundary(selected_modules: list[dict[str, Any]] | None, integration: str) -> str:
    modules = selected_modules or []
    names = [_first(module.get("name") or module.get("id"), "") for module in modules]
    names = [name for name in names if name]
    integration_label = _integration_label(integration)
    if names:
        return f"the {integration_label} through the {', '.join(names)} module boundary"
    return f"{integration_label} boundary"


def _behavior_criterion(family: str, workflow: str) -> str:
    if family == "mobile":
        return f"Build a user-facing screen or UI flow for {workflow} with the primary happy path observable."
    if family == "backend":
        return f"Build an endpoint or service path for {workflow} with the primary happy path observable."
    return f"Build a route or page for {workflow} with the primary happy path observable."


def generate_vertical_slice_tasks(
    *,
    picked: dict[str, Any],
    archetype: dict[str, Any] | None = None,
    selected_modules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic first vertical-slice task proposals.

    The generator is intentionally pure: callers pass confirmed init choices in,
    and receive reviewable task dictionaries without filesystem or DB writes.
    """
    intent = picked.get("init_intent") or {}
    workflow = _first(intent.get("primary_workflows"), "first useful workflow")
    entity = _first(intent.get("data_needs"), "the core data entity")
    integration = _first(intent.get("integrations"), "the first external or internal dependency")
    quality = _first(intent.get("quality_priorities"), "the highest-priority quality concern")
    family = _family(picked=picked, archetype=archetype, intent=intent)
    boundary = _module_boundary(selected_modules, integration)

    return [
        {
            "id": f"vertical-slice-{family}-{_slug(workflow)}",
            "summary": f"Ship first {family} vertical slice for {workflow}",
            "description": (
                f"Create the first end-to-end {family} slice for {workflow}, connecting the user-visible "
                f"behavior to {entity}, {integration}, and {quality} so the project starts with a real "
                "workflow instead of only setup."
            ),
            "priority": "High",
            "task_type": "feature",
            "complexity": "M",
            "criteria": [
                _behavior_criterion(family, workflow),
                f"Handle {entity} through the slice, including the minimal schema or state needed for the workflow.",
                f"Connect {boundary} and document any stubbed or deferred behavior.",
                f"Add automated or manual test coverage that verifies {workflow} with {quality} in mind.",
                "Document the handoff notes needed for the next task to extend this vertical slice.",
            ],
            "source": "init_vertical_slice",
        }
    ]


def _parse_json_arg(raw: str | None, label: str, default):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--{label} is not valid JSON: {exc}") from exc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk init-vertical-slice",
        description="Generate first vertical-slice task proposals without side effects.",
    )
    parser.add_argument("--picked", required=True, help="Picked init-wizard config JSON object.")
    parser.add_argument("--archetype", default=None, help="Inferred archetype JSON object.")
    parser.add_argument("--selected-modules", default=None, help="Selected modules JSON array.")
    args = parser.parse_args(argv[2:])

    try:
        picked = _parse_json_arg(args.picked, "picked", {})
        archetype = _parse_json_arg(args.archetype, "archetype", {})
        selected_modules = _parse_json_arg(args.selected_modules, "selected-modules", [])
        if not isinstance(picked, dict):
            raise ValueError("--picked must be a JSON object")
        if not isinstance(archetype, dict):
            raise ValueError("--archetype must be a JSON object")
        if not isinstance(selected_modules, list):
            raise ValueError("--selected-modules must be a JSON array")
        if not all(isinstance(item, dict) for item in selected_modules):
            raise ValueError("--selected-modules must be a JSON array of objects")
        tasks = generate_vertical_slice_tasks(
            picked=picked,
            archetype=archetype,
            selected_modules=selected_modules,
        )
    except ValueError as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, "tasks": tasks}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-vertical-slice [options]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
