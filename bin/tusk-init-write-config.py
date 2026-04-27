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
    --domains <json_array>       JSON array of domain strings, e.g. '["api","frontend"]'
    --agents <json_object>       JSON object mapping agent name to config, e.g. '{"backend":{"model":"sonnet"}}'
    --task-types <json_array>    JSON array of task type strings, e.g. '["bug","feature"]'
    --test-command <string>      Test command string, or empty string to clear
    --project-type <string>      Project type identifier, or empty string to set null
    --project-libs <json_object> JSON object mapping lib name to {repo, ref}, e.g. '{"ios_app":{"repo":"gioe/ios-libs","ref":"main"}}'

Output (JSON):
    {"success": true, "config_path": "/path/to/config.json", "backed_up": true}
    {"success": false, "config_path": "/path/to/config.json", "backed_up": true, "error": "..."}
"""

import argparse
import json
import os
import shutil
import subprocess
import sys


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk-init-write-config.py <db_path> <config_path> [options]", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--domains", default=None)
    parser.add_argument("--agents", default=None)
    parser.add_argument("--task-types", default=None)
    parser.add_argument("--test-command", default=None)
    parser.add_argument("--project-type", default=None)
    parser.add_argument("--project-libs", default=None)
    args, _ = parser.parse_known_args(sys.argv[3:])

    # ── Load existing config ──
    existing = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({
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
            print(json.dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--domains is not valid JSON: {e}",
            }))
            return
        if not isinstance(domains, list):
            print(json.dumps({
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
            print(json.dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--agents is not valid JSON: {e}",
            }))
            return
        if not isinstance(agents, dict):
            print(json.dumps({
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
            print(json.dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--task-types is not valid JSON: {e}",
            }))
            return
        if not isinstance(task_types, list):
            print(json.dumps({
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

    if args.project_libs is not None:
        try:
            project_libs = json.loads(args.project_libs)
        except json.JSONDecodeError as e:
            print(json.dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": f"--project-libs is not valid JSON: {e}",
            }))
            return
        if not isinstance(project_libs, dict):
            print(json.dumps({
                "success": False,
                "config_path": config_path,
                "backed_up": False,
                "error": "--project-libs must be a JSON object",
            }))
            return
        updates["project_libs"] = project_libs
    elif args.project_type:
        # Auto-populate project_libs from config.default.json when --project-type is a
        # known built-in type and --project-libs was not explicitly provided.
        default_config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.default.json"
        )
        try:
            with open(default_config_path) as f:
                default_config = json.load(f)
            default_libs = default_config.get("project_libs", {})
            if args.project_type in default_libs:
                # Carry forward existing project_libs, then add the matched entry.
                merged_libs = dict(existing.get("project_libs") or {})
                merged_libs[args.project_type] = default_libs[args.project_type]
                updates["project_libs"] = merged_libs
        except (OSError, json.JSONDecodeError):
            pass  # silently ignore if config.default.json is missing or invalid

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
            print(json.dumps({
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
        print(json.dumps({
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
    if os.path.isfile(db_path):
        refresh_cmd = ["tusk", "regen-triggers"]
    else:
        refresh_cmd = ["tusk", "init"]

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
        print(json.dumps({
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
        print(json.dumps({
            "success": False,
            "config_path": config_path,
            "backed_up": backed_up,
            "error": error_msg,
        }))
        return

    print(json.dumps({
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
