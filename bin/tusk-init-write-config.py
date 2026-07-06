#!/usr/bin/env python3
"""Merge config values and refresh validation triggers.

Reads the existing tusk/config.json, merges the provided values (carrying
forward any key the user has not explicitly set), backs up the existing
config, writes the new config, then refreshes the DB's validation triggers
to match the new config — without touching task data. On any failure, the
config backup is restored.

Trigger refresh dispatch:
- DB exists  → `tusk regen-triggers` (drops and recreates `validate_*`
  triggers from the updated config; preserves all rows in `tasks`,
  `acceptance_criteria`, `task_sessions`, `skill_runs`, etc.).
- DB missing → `tusk init` (creates a fresh DB; the wizard's normal
  prerequisite is that the DB already exists, but this fallback keeps
  init-write-config usable even if the DB has been deleted manually).

This is a config-only operation. It must never destroy task history —
issue #604 was filed when an earlier implementation called
`tusk init --force` unconditionally and silently wiped populated DBs.

Usage:
    tusk-init-write-config.py <db_path> <config_path> [options]

Options:
    --domains <json_array>                JSON array of domain strings, e.g. '["api","frontend"]'
    --agents <json_object>                JSON object mapping agent name to config, e.g. '{"backend":{"model":"sonnet"}}'
    --task-types <json_array>             JSON array of task type strings, e.g. '["bug","feature"]'
    --test-command <string>               Test command string, or empty string to clear
    --project-type <string>               Project type identifier, or empty string to set null
    --init-intent <json_object>           Normalized project-intent record from tusk init-intent
    --project-libs <json_object>          JSON object mapping lib name to {repo, ref}, e.g. '{"ios_app":{"repo":"gioe/ios-libs","ref":"main"}}'
    --worktree-symlink-files <json_array> JSON array of basenames to auto-symlink from the primary checkout into new task worktrees, e.g. '[".venv",".env"]'

Output (JSON):
    {"success": true, "config_path": "/path/to/config.json", "backed_up": true}
    {"success": false, "config_path": "/path/to/config.json", "backed_up": true, "error": "..."}
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys

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


def _select_project_libs(project_type, init_intent, existing_project_libs):
    selector = _load_script_module("tusk-init-bootstrap-select.py", "tusk_init_bootstrap_select")
    archetype = {}
    if init_intent:
        try:
            intent_helper = _load_script_module("tusk-init-intent.py", "tusk_init_intent")
            archetype = intent_helper.infer_archetype(init_intent)
        except Exception:
            archetype = {}
    return selector.select_bootstrap_packs(
        project_type=project_type,
        intent=init_intent or {},
        archetype=archetype,
        existing_project_libs=existing_project_libs or {},
    )


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk-init-write-config.py <db_path> <config_path> [options]", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--domains", default=None)
    parser.add_argument("--agents", default=None)
    parser.add_argument("--task-types", default=None)
    parser.add_argument("--test-command", default=None)
    parser.add_argument("--project-type", default=None)
    parser.add_argument("--init-intent", default=None)
    parser.add_argument("--project-libs", default=None)
    parser.add_argument("--worktree-symlink-files", default=None)
    args, _ = parser.parse_known_args(sys.argv[3:])

    # Only types whose suggestion differs from the empty default appear here;
    # types absent from the map carry `worktree.symlink_files` forward unchanged.
    WORKTREE_SYMLINK_DEFAULTS = {
        "python_service": [".venv", ".env"],
        "web_app": ["node_modules", ".env", ".env.local"],
    }

    # ── Load existing config ──
    existing = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"Failed to read existing config: {e}",
            }))
            return

    # ── Parse provided values ──
    updates = {}

    if args.domains is not None:
        try:
            domains = json.loads(args.domains)
        except json.JSONDecodeError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--domains is not valid JSON: {e}",
            }))
            return
        if not isinstance(domains, list):
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--domains must be a JSON array",
            }))
            return
        updates["domains"] = domains

    if args.agents is not None:
        try:
            agents = json.loads(args.agents)
        except json.JSONDecodeError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--agents is not valid JSON: {e}",
            }))
            return
        if not isinstance(agents, dict):
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--agents must be a JSON object",
            }))
            return
        updates["agents"] = agents

    if args.task_types is not None:
        try:
            task_types = json.loads(args.task_types)
        except json.JSONDecodeError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--task-types is not valid JSON: {e}",
            }))
            return
        if not isinstance(task_types, list):
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--task-types must be a JSON array",
            }))
            return
        updates["task_types"] = task_types

    if args.test_command is not None:
        updates["test_command"] = args.test_command

    if args.project_type is not None:
        updates["project_type"] = args.project_type if args.project_type != "" else None

    if args.init_intent is not None:
        try:
            init_intent = json.loads(args.init_intent)
        except json.JSONDecodeError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--init-intent is not valid JSON: {e}",
            }))
            return
        if not isinstance(init_intent, dict):
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--init-intent must be a JSON object",
            }))
            return
        updates["init_intent"] = init_intent

    project_libs_explicit = args.project_libs is not None
    if args.project_libs is not None:
        try:
            project_libs = json.loads(args.project_libs)
        except json.JSONDecodeError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--project-libs is not valid JSON: {e}",
            }))
            return
        if not isinstance(project_libs, dict):
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--project-libs must be a JSON object",
            }))
            return
        updates["project_libs"] = project_libs

    should_auto_select_project_libs = (
        not project_libs_explicit
        and (args.project_type is not None or args.init_intent is not None)
    )
    if should_auto_select_project_libs:
        selected_project_type = updates.get("project_type", existing.get("project_type"))
        selected_intent = updates.get("init_intent", existing.get("init_intent"))
        selected = _select_project_libs(
            selected_project_type,
            selected_intent,
            existing.get("project_libs") or {},
        )
        if selected["project_libs"] != (existing.get("project_libs") or {}):
            updates["project_libs"] = selected["project_libs"]

    if args.worktree_symlink_files is not None:
        try:
            wsf = json.loads(args.worktree_symlink_files)
        except json.JSONDecodeError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--worktree-symlink-files is not valid JSON: {e}",
            }))
            return
        if not isinstance(wsf, list) or not all(isinstance(x, str) for x in wsf):
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--worktree-symlink-files must be a JSON array of strings",
            }))
            return
        merged_worktree = dict(existing.get("worktree") or {})
        merged_worktree["symlink_files"] = wsf
        updates["worktree"] = merged_worktree
    elif args.project_type and args.project_type in WORKTREE_SYMLINK_DEFAULTS:
        # Only seed when the existing list is missing or empty so re-runs
        # preserve any user customization — mirrors the project_libs merge
        # semantics above (defaults augment, never overwrite).
        existing_worktree = dict(existing.get("worktree") or {})
        if not existing_worktree.get("symlink_files"):
            existing_worktree["symlink_files"] = list(WORKTREE_SYMLINK_DEFAULTS[args.project_type])
            updates["worktree"] = existing_worktree

    # ── Merge: existing config wins for keys not provided ──
    merged = dict(existing)
    merged.update(updates)

    # ── Back up existing config ──
    backup_path = config_path + ".bak"
    backed_up = False
    if os.path.isfile(config_path):
        try:
            shutil.copy2(config_path, backup_path)
            backed_up = True
        except OSError as e:
            print(dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"Failed to back up config: {e}",
            }))
            return

    # ── Write new config ──
    try:
        config_dir = os.path.dirname(config_path)
        if config_dir:
            os.makedirs(config_dir, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
    except OSError as e:
        # Restore backup if write fails
        if backed_up:
            try:
                shutil.copy2(backup_path, config_path)
            except OSError:
                pass
        print(dumps({
            "success": False,
            "config_path": config_path,
            "backed_up": backed_up,
            "error": f"Failed to write config: {e}",
        }))
        return

    # ── Refresh validation triggers from the new config ──
    # When the DB already exists, use `tusk regen-triggers` so existing task
    # data is preserved (issue #604). Only fall back to `tusk init` when the
    # DB is missing entirely — and then without --force, which is reserved
    # for the explicit "destroy and recreate" path that the wizard must
    # never invoke implicitly.
    db_path = sys.argv[1]
    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")
    if os.path.isfile(db_path):
        refresh_cmd = [tusk_bin, "regen-triggers"]
    else:
        refresh_cmd = [tusk_bin, "init"]

    try:
        result = subprocess.run(
            refresh_cmd,
            capture_output=True,
            text=True, encoding="utf-8",
        )
    except FileNotFoundError:
        # Restore backup
        if backed_up:
            try:
                shutil.copy2(backup_path, config_path)
            except OSError:
                pass
        print(dumps({
            "success": False,
            "config_path": config_path,
            "backed_up": backed_up,
            "error": "tusk command not found",
        }))
        return

    if result.returncode != 0:
        error_msg = (result.stderr or result.stdout or f"{' '.join(refresh_cmd)} failed").strip()
        # Restore config backup
        if backed_up:
            try:
                shutil.copy2(backup_path, config_path)
            except OSError:
                pass
        print(dumps({
            "success": False,
            "config_path": config_path,
            "backed_up": backed_up,
            "error": error_msg,
        }))
        return

    print(dumps({
        "success": True,
        "config_path": config_path,
        "backed_up": backed_up,
    }))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-write-config", file=sys.stderr)
        sys.exit(1)
    main()
