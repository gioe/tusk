#!/usr/bin/env python3
"""Fetch tusk-bootstrap.json for each configured project lib.

Reads project_libs from tusk config, fetches each lib's tusk-bootstrap.json
from GitHub via `gh api`, and returns structured JSON.

Usage:
    tusk-init-fetch-bootstrap.py <db_path> <config_path>

Output (JSON):
    {
      "libs": [
        {
          "name": "ios_app",
          "repo": "gioe/ios-libs",
          "tasks": [...],
          "error": null
        },
        {
          "name": "bad_lib",
          "repo": "owner/repo",
          "tasks": [],
          "error": "404: tusk-bootstrap.json not found"
        }
      ]
    }

Each lib entry always has: name, repo, tasks (list), error (str or null).
"""

import base64
import json
import subprocess
import sys


REQUIRED_TOP_LEVEL = {"version", "project_type", "tasks"}
REQUIRED_TASK_FIELDS = {"summary", "description", "priority", "task_type", "complexity", "criteria"}


def _fetch_bootstrap(repo: str, ref: str) -> tuple:
    """Fetch and decode tusk-bootstrap.json. Returns (data_dict, error_str)."""
    url = f"repos/{repo}/contents/tusk-bootstrap.json?ref={ref}"
    try:
        result = subprocess.run(
            ["gh", "api", url, "--jq", ".content"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return None, "gh not available"
    except subprocess.TimeoutExpired:
        return None, "gh api timed out"

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "404" in stderr or "Not Found" in stderr:
            return None, "404: tusk-bootstrap.json not found"
        msg = stderr or f"gh api exited {result.returncode}"
        return None, msg

    raw_content = result.stdout.strip()
    if not raw_content:
        return None, "empty response from gh api"

    # .content from GitHub API is base64 with newlines; decode it
    try:
        decoded = base64.b64decode(raw_content).decode("utf-8")
    except Exception as e:
        return None, f"base64 decode error: {e}"

    try:
        data = json.loads(decoded)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"

    return data, None


def _validate(data: dict) -> str | None:
    """Validate required keys. Returns error string or None."""
    if not isinstance(data, dict):
        return "bootstrap file is not a JSON object"

    missing_top = REQUIRED_TOP_LEVEL - data.keys()
    if missing_top:
        return f"missing required keys: {sorted(missing_top)}"

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return "tasks must be an array"

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            return f"tasks[{i}] is not an object"
        missing = REQUIRED_TASK_FIELDS - task.keys()
        if missing:
            return f"tasks[{i}] missing required fields: {sorted(missing)}"
        criteria = task.get("criteria")
        if not isinstance(criteria, list) or len(criteria) == 0:
            return f"tasks[{i}].criteria must be a non-empty array"

    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk-init-fetch-bootstrap.py <db_path> <config_path>", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[2]

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading config: {e}", file=sys.stderr)
        sys.exit(1)

    project_libs = config.get("project_libs") or {}
    if not project_libs:
        print(json.dumps({"libs": []}))
        return

    libs_out = []

    for name, lib_cfg in project_libs.items():
        repo = lib_cfg.get("repo", "")
        ref = lib_cfg.get("ref", "main")

        if not repo:
            libs_out.append({"name": name, "repo": repo, "tasks": [], "error": "missing repo in config"})
            continue

        data, fetch_err = _fetch_bootstrap(repo, ref)
        if fetch_err:
            libs_out.append({"name": name, "repo": repo, "tasks": [], "error": fetch_err})
            continue

        val_err = _validate(data)
        if val_err:
            libs_out.append({"name": name, "repo": repo, "tasks": [], "error": f"invalid bootstrap: {val_err}"})
            continue

        libs_out.append({"name": name, "repo": repo, "tasks": data["tasks"], "error": None})

    print(json.dumps({"libs": libs_out}))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-fetch-bootstrap", file=sys.stderr)
        sys.exit(1)
    main()
