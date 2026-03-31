#!/usr/bin/env python3
"""Add a project lib to tusk config and fetch its bootstrap tasks.

Usage:
    tusk add-lib [--lib <name>] [--repo <owner/repo>] [--ref <branch|tag|sha>]

When --lib is a known built-in (ios_app, python_service) and --repo is not provided,
the lib's repo and ref are loaded from config.default.json.

When --repo is provided, a custom lib entry is added using the given name (--lib required).

Output (JSON):
    {"lib": "<name>", "tasks": [...], "error": null}
    {"lib": "<name>", "tasks": [], "error": "<error message>"}
"""

import argparse
import json
import os
import subprocess
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_default_project_libs() -> dict:
    """Load project_libs from config.default.json (installed dir or one level up)."""
    for candidate in [SCRIPT_DIR, os.path.dirname(SCRIPT_DIR)]:
        path = os.path.join(candidate, "config.default.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get("project_libs") or {}
            except (OSError, json.JSONDecodeError):
                pass
    return {}


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk-add-lib.py <db_path> <config_path> [options]", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lib", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--ref", default=None)
    args, _ = parser.parse_known_args(sys.argv[3:])

    lib_name = args.lib
    repo = args.repo
    ref = args.ref

    # Validate: at least one of --lib or --repo is required
    if not lib_name and not repo:
        print(json.dumps({"lib": None, "tasks": [], "error": "provide --lib <name> or --repo <owner/repo>"}))
        sys.exit(1)

    default_libs = _load_default_project_libs()

    # Determine lib name and entry
    if repo:
        # Custom lib — --lib required to name it
        if not lib_name:
            print(json.dumps({"lib": None, "tasks": [], "error": "--lib <name> is required when using --repo"}))
            sys.exit(1)
        lib_entry = {"repo": repo, "ref": ref or "main"}
    elif lib_name in default_libs:
        # Known built-in — merge from config.default.json, allow --ref override
        defaults = default_libs[lib_name]
        lib_entry = {"repo": defaults["repo"], "ref": ref or defaults.get("ref", "main")}
    else:
        known = sorted(default_libs.keys())
        hint = f"known built-ins: {known}" if known else "no built-ins found in config.default.json"
        print(json.dumps({
            "lib": lib_name,
            "tasks": [],
            "error": f"unknown built-in lib '{lib_name}'; provide --repo to add a custom lib ({hint})",
        }))
        sys.exit(1)

    # Load current config
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"lib": lib_name, "tasks": [], "error": f"failed to read config: {e}"}))
        sys.exit(1)

    # Merge into project_libs (no DB reinit)
    project_libs = config.get("project_libs") or {}
    project_libs[lib_name] = lib_entry
    config["project_libs"] = project_libs

    # Write updated config
    try:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(json.dumps({"lib": lib_name, "tasks": [], "error": f"failed to write config: {e}"}))
        sys.exit(1)

    # Fetch bootstrap tasks for this lib via tusk init-fetch-bootstrap
    try:
        result = subprocess.run(
            ["tusk", "init-fetch-bootstrap"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        print(json.dumps({"lib": lib_name, "tasks": [], "error": "tusk not found in PATH"}))
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(json.dumps({"lib": lib_name, "tasks": [], "error": "init-fetch-bootstrap timed out"}))
        sys.exit(1)

    if result.returncode != 0:
        err = result.stderr.strip() or f"init-fetch-bootstrap exited {result.returncode}"
        print(json.dumps({"lib": lib_name, "tasks": [], "error": err}))
        sys.exit(1)

    try:
        bootstrap_out = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(json.dumps({"lib": lib_name, "tasks": [], "error": f"failed to parse bootstrap output: {e}"}))
        sys.exit(1)

    # Find the entry for this lib in the bootstrap output
    lib_result = next(
        (entry for entry in bootstrap_out.get("libs", []) if entry["name"] == lib_name),
        None,
    )
    if lib_result is None:
        print(json.dumps({"lib": lib_name, "tasks": [], "error": "lib not found in bootstrap output"}))
        sys.exit(1)

    print(json.dumps({
        "lib": lib_name,
        "tasks": lib_result.get("tasks", []),
        "error": lib_result.get("error"),
    }))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk add-lib [--lib <name>] [--repo <owner/repo>] [--ref <branch|tag|sha>]", file=sys.stderr)
        sys.exit(1)
    main()
