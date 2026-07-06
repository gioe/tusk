#!/usr/bin/env python3
"""Build a reviewable tusk-init bootstrap plan without side effects."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_script_module(filename: str, module_name: str):
    path = os.path.join(SCRIPT_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _signals(picked: dict[str, Any], archetype: dict[str, Any] | None) -> dict[str, set[str]]:
    intent = picked.get("init_intent") or {}
    archetype = archetype or {}
    return {
        "project_types": set(_list(picked.get("project_type") or intent.get("project_type"))),
        "archetypes": set(_list(archetype.get("id") or archetype.get("archetype"))),
        "platforms": set(_list(intent.get("platforms"))),
        "features": set(
            _list(intent.get("stack_preferences"))
            + _list(intent.get("integrations"))
            + _list(intent.get("quality_priorities"))
            + _list(intent.get("primary_workflows"))
        ),
    }


def _match_module(module: dict[str, Any], signals: dict[str, set[str]]) -> tuple[bool, list[str]]:
    applicability = module.get("applicability") or {}
    reasons: list[str] = []
    for key, label in (
        ("project_types", "project_type"),
        ("archetypes", "archetype"),
        ("platforms", "platform"),
        ("requires", "requires"),
    ):
        wanted = set(_list(applicability.get(key)))
        if not wanted:
            continue
        signal_key = "features" if key == "requires" else key
        matched = sorted(wanted & signals[signal_key])
        if not matched:
            return False, []
        reasons.extend([f"{label}={value}" for value in matched])

    excluded = set(_list(applicability.get("excludes")))
    if excluded and excluded & signals["features"]:
        return False, []

    if not applicability:
        return True, ["operator-added"]
    return True, reasons


def _with_module(entry: dict[str, Any], module_id: str) -> list[dict[str, Any]]:
    return [dict(item, module=module_id) for item in entry or []]


def _task_with_source(task: dict[str, Any], source: str) -> dict[str, Any]:
    out = dict(task)
    out["source"] = source
    return out


def build_bootstrap_plan(
    *,
    picked: dict[str, Any],
    archetype: dict[str, Any] | None = None,
    bootstrap: dict[str, Any] | None = None,
    scaffold_spec: list[dict[str, Any]] | None = None,
    remove_modules: list[str] | None = None,
    add_modules: list[dict[str, Any]] | None = None,
    plan_action: str = "accept",
) -> dict[str, Any]:
    bootstrap = bootstrap or {"libs": []}
    remove_set = set(_list(remove_modules))
    signals = _signals(picked, archetype)

    selector = _load_script_module("tusk-init-bootstrap-select.py", "tusk_init_bootstrap_select")
    selection = selector.select_bootstrap_packs(
        project_type=picked.get("project_type"),
        intent=picked.get("init_intent") or {},
        archetype=archetype or {},
        existing_project_libs=picked.get("project_libs") or {},
    )

    materialize = plan_action != "skip-materialization"
    selected_modules: list[dict[str, Any]] = []
    skipped_modules: list[dict[str, Any]] = []
    files_to_write: list[dict[str, Any]] = []
    context_atoms: list[dict[str, Any]] = []
    pillars: list[dict[str, Any]] = []
    glossary: list[dict[str, Any]] = []
    tasks_to_create: list[dict[str, Any]] = []

    if materialize:
        for lib in bootstrap.get("libs") or []:
            lib_name = lib.get("name") or ""
            if lib.get("error"):
                skipped_modules.append({
                    "id": f"lib:{lib_name}",
                    "name": lib_name,
                    "lib": lib_name,
                    "reason": lib["error"],
                })
                continue
            for entry in lib.get("manifest_files") or []:
                files_to_write.append(dict(entry, source=f"lib:{lib_name}"))
            for task in lib.get("tasks") or []:
                tasks_to_create.append(_task_with_source(task, f"lib:{lib_name}"))
            for module in lib.get("modules") or []:
                module_id = module.get("id") or ""
                matches, reasons = _match_module(module, signals)
                if module_id.lower() in remove_set:
                    skipped_modules.append({
                        "id": module_id,
                        "name": module.get("name", module_id),
                        "lib": lib_name,
                        "reason": "removed by plan edit",
                    })
                    continue
                if not matches:
                    skipped_modules.append({
                        "id": module_id,
                        "name": module.get("name", module_id),
                        "lib": lib_name,
                        "reason": "applicability rules did not match",
                    })
                    continue
                selected_modules.append({
                    "id": module_id,
                    "name": module.get("name", module_id),
                    "description": module.get("description", ""),
                    "lib": lib_name,
                    "matched": reasons,
                })
                for field in ("files", "optional_files", "append_operations"):
                    files_to_write.extend(_with_module(module.get(field) or [], module_id))
                context_atoms.extend(_with_module(module.get("context_atoms") or [], module_id))
                pillars.extend(_with_module(module.get("pillars") or [], module_id))
                glossary.extend(_with_module(module.get("glossary") or [], module_id))
                for task in module.get("tasks") or []:
                    tasks_to_create.append(_task_with_source(task, f"module:{module_id}"))

        for module in add_modules or []:
            module_id = module.get("id") or ""
            selected_modules.append({
                "id": module_id,
                "name": module.get("name", module_id),
                "description": module.get("description", ""),
                "lib": module.get("lib", "manual"),
                "matched": ["operator-added"],
            })
            for field in ("files", "optional_files", "append_operations"):
                files_to_write.extend(_with_module(module.get(field) or [], module_id))
            context_atoms.extend(_with_module(module.get("context_atoms") or [], module_id))
            pillars.extend(_with_module(module.get("pillars") or [], module_id))
            glossary.extend(_with_module(module.get("glossary") or [], module_id))
            for task in module.get("tasks") or []:
                tasks_to_create.append(_task_with_source(task, f"module:{module_id}"))

    return {
        "intent": {
            "project_type": picked.get("project_type"),
            "init_intent": picked.get("init_intent"),
            "domains": picked.get("domains"),
            "agents": picked.get("agents"),
            "task_types": picked.get("task_types"),
            "test_command": picked.get("test_command"),
            "worktree_symlink_files": picked.get("worktree_symlink_files"),
        },
        "archetype": archetype or {},
        "utility_repos": selection.get("project_libs") or {},
        "selected_utility_modules": selection.get("selected_modules") or [],
        "skipped_utility_modules": selection.get("skipped_modules") or [],
        "selected_modules": selected_modules,
        "skipped_modules": skipped_modules,
        "scaffold": list(scaffold_spec or []) if materialize else [],
        "files_to_write": files_to_write if materialize else [],
        "context_atoms": context_atoms if materialize else [],
        "pillars": pillars if materialize else [],
        "glossary": glossary if materialize else [],
        "tasks_to_create": tasks_to_create if materialize else [],
        "actions": {
            "materialize": materialize,
            "plan_action": plan_action,
        },
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
        prog="tusk init-bootstrap-plan",
        description="Build a reviewable tusk-init bootstrap plan without side effects.",
    )
    parser.add_argument("--picked", required=True, help="Picked init-wizard config JSON object.")
    parser.add_argument("--archetype", default=None, help="Inferred archetype JSON object.")
    parser.add_argument("--bootstrap", default=None, help="init-fetch-bootstrap JSON payload.")
    parser.add_argument("--scaffold-spec", default=None, help="Scaffold spec JSON array.")
    parser.add_argument("--remove-module", "--plan-remove-module", action="append", default=[])
    parser.add_argument(
        "--add-module",
        "--plan-add-module",
        action="append",
        default=[],
        help="Module JSON object to add.",
    )
    parser.add_argument(
        "--plan-action",
        choices=["accept", "skip-materialization"],
        default="accept",
    )
    args = parser.parse_args(argv[2:])

    try:
        picked = _parse_json_arg(args.picked, "picked", {})
        archetype = _parse_json_arg(args.archetype, "archetype", {})
        bootstrap = _parse_json_arg(args.bootstrap, "bootstrap", {"libs": []})
        scaffold_spec = _parse_json_arg(args.scaffold_spec, "scaffold-spec", [])
        add_modules = [_parse_json_arg(raw, "add-module", {}) for raw in args.add_module]
        if not isinstance(picked, dict):
            raise ValueError("--picked must be a JSON object")
        if not isinstance(archetype, dict):
            raise ValueError("--archetype must be a JSON object")
        if not isinstance(bootstrap, dict):
            raise ValueError("--bootstrap must be a JSON object")
        if not isinstance(scaffold_spec, list):
            raise ValueError("--scaffold-spec must be a JSON array")
        if not all(isinstance(item, dict) for item in add_modules):
            raise ValueError("--add-module must be a JSON object")
        plan = build_bootstrap_plan(
            picked=picked,
            archetype=archetype,
            bootstrap=bootstrap,
            scaffold_spec=scaffold_spec,
            remove_modules=args.remove_module,
            add_modules=add_modules,
            plan_action=args.plan_action,
        )
    except ValueError as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, "plan": plan}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-bootstrap-plan [options]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
