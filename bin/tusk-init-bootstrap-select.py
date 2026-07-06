#!/usr/bin/env python3
"""Select project-lib bootstrap packs from init intent and archetype signals."""

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


DEFAULT_PACK_CATALOG = {
    "ios_app": {
        "repo": "gioe/ios-libs",
        "ref": "main",
        "applicability": {
            "project_types": ["ios_app"],
            "archetypes": ["consumer_ios_app"],
            "platforms": ["ios"],
            "features": ["swiftui", "mobile"],
        },
    },
    "python_service": {
        "repo": "gioe/python-libs",
        "ref": "main",
        "applicability": {
            "project_types": ["python_service"],
            "archetypes": ["api_service"],
            "platforms": ["backend"],
            "features": ["api", "python", "observability"],
        },
    },
    "android_app": {
        "repo": None,
        "ref": "main",
        "optional": True,
        "applicability": {
            "project_types": ["android_app"],
            "platforms": ["android"],
            "features": ["mobile"],
        },
    },
    "web_app": {
        "repo": None,
        "ref": "main",
        "optional": True,
        "applicability": {
            "project_types": ["web_app"],
            "archetypes": ["internal_tool", "b2b_dashboard", "content_site"],
            "platforms": ["web"],
            "features": ["dashboard", "frontend"],
        },
    },
    "backend": {
        "repo": None,
        "ref": "main",
        "optional": True,
        "applicability": {
            "archetypes": ["api_service", "b2b_dashboard"],
            "platforms": ["backend"],
            "features": ["api", "auth", "database"],
        },
    },
}


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
    seen: set[str] = set()
    for raw in raw_items:
        text = str(raw).strip().lower()
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items


def _signals(
    *,
    project_type: str | None,
    intent: dict[str, Any] | None,
    archetype: dict[str, Any] | None,
    selected_features: list[str] | None,
) -> dict[str, set[str]]:
    intent = intent or {}
    archetype = archetype or {}
    return {
        "project_types": set(_list(project_type or intent.get("project_type"))),
        "archetypes": set(_list(archetype.get("id") or archetype.get("archetype"))),
        "platforms": set(_list(intent.get("platforms"))),
        "features": set(
            _list(selected_features)
            + _list(intent.get("stack_preferences"))
            + _list(intent.get("integrations"))
            + _list(intent.get("quality_priorities"))
            + _list(intent.get("primary_workflows"))
        ),
    }


def _matched_reasons(applicability: dict[str, Any], signals: dict[str, set[str]]) -> list[str]:
    reasons: list[str] = []
    for key in ("project_types", "archetypes", "platforms", "features"):
        wanted = set(_list(applicability.get(key)))
        if not wanted:
            continue
        matched = sorted(wanted & signals[key])
        for value in matched:
            label = {
                "project_types": "project_type",
                "archetypes": "archetype",
                "platforms": "platform",
                "features": "feature",
            }[key]
            reasons.append(f"{label}={value}")
    return reasons


def select_bootstrap_packs(
    *,
    project_type: str | None = None,
    intent: dict[str, Any] | None = None,
    archetype: dict[str, Any] | None = None,
    selected_features: list[str] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
    existing_project_libs: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    catalog = catalog or DEFAULT_PACK_CATALOG
    selected_signals = _signals(
        project_type=project_type,
        intent=intent,
        archetype=archetype,
        selected_features=selected_features,
    )
    project_libs = dict(existing_project_libs or {})
    selected_modules: list[dict[str, Any]] = []
    skipped_modules: list[dict[str, str]] = []

    for name, pack in catalog.items():
        reasons = _matched_reasons(pack.get("applicability") or {}, selected_signals)
        if not reasons:
            continue

        repo = pack.get("repo")
        if not repo:
            if pack.get("optional", True):
                skipped_modules.append({
                    "name": name,
                    "reason": "optional utility repo is not configured",
                })
                continue
            skipped_modules.append({"name": name, "reason": "required utility repo is not configured"})
            continue

        ref = pack.get("ref") or "main"
        project_libs[name] = {"repo": repo, "ref": ref}
        selected_modules.append({
            "name": name,
            "repo": repo,
            "ref": ref,
            "matched": reasons,
        })

    return {
        "project_libs": project_libs,
        "selected_modules": selected_modules,
        "skipped_modules": skipped_modules,
    }


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
        prog="tusk init-bootstrap-select",
        description="Select utility bootstrap packs from init intent and archetype signals.",
    )
    parser.add_argument("--project-type", default=None)
    parser.add_argument("--intent", default=None, help="Normalized init_intent JSON object.")
    parser.add_argument("--archetype", default=None, help="Archetype JSON object from init-intent archetype.")
    parser.add_argument("--features", default=None, help="JSON array of selected feature strings.")
    parser.add_argument("--catalog", default=None, help="Optional pack catalog JSON object.")
    parser.add_argument("--existing-project-libs", default=None, help="Existing project_libs JSON object.")
    args = parser.parse_args(argv[2:])

    try:
        intent = _parse_json_arg(args.intent, "intent", {})
        archetype = _parse_json_arg(args.archetype, "archetype", {})
        features = _parse_json_arg(args.features, "features", [])
        catalog = _parse_json_arg(args.catalog, "catalog", None)
        existing = _parse_json_arg(args.existing_project_libs, "existing-project-libs", {})
        if not isinstance(intent, dict):
            raise ValueError("--intent must be a JSON object")
        if not isinstance(archetype, dict):
            raise ValueError("--archetype must be a JSON object")
        if not isinstance(features, list):
            raise ValueError("--features must be a JSON array")
        if catalog is not None and not isinstance(catalog, dict):
            raise ValueError("--catalog must be a JSON object")
        if not isinstance(existing, dict):
            raise ValueError("--existing-project-libs must be a JSON object")
        selection = select_bootstrap_packs(
            project_type=args.project_type,
            intent=intent,
            archetype=archetype,
            selected_features=features,
            catalog=catalog,
            existing_project_libs=existing,
        )
    except ValueError as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, "selection": selection}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-bootstrap-select [options]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
