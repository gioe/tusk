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
          "modules": [...],
          "manifest_files": [...],
          "manifest_schema_version": 2,
          "error": null
        },
        {
          "name": "bad_lib",
          "repo": "owner/repo",
          "tasks": [],
          "modules": [],
          "manifest_files": [],
          "error": "404: tusk-bootstrap.json not found"
        }
      ]
    }

Each lib entry always has: name, repo, tasks (list), modules (list),
manifest_files (list), manifest_schema_version (int), error (str or null).
"""

import base64
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-path-lib.py and tusk-json-lib.py

_path_lib = tusk_loader.load("tusk-path-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
validate_relative_path = _path_lib.validate_relative_path
dumps = _json_lib.dumps


REQUIRED_TOP_LEVEL = {"version", "project_type", "tasks"}
REQUIRED_TASK_FIELDS = {"summary", "description", "priority", "task_type", "complexity", "criteria"}
REQUIRED_MODULE_FIELDS = {"id", "name", "description"}
VALID_MANIFEST_MODES = {"create_only", "append_if_missing", "marker_block"}
VALID_APPLICABILITY_KEYS = {
    "project_types",
    "archetypes",
    "platforms",
    "requires",
    "excludes",
}
VALID_CONTEXT_TYPES = {"memory", "assumption", "question", "risk", "decision", "entry_point"}


def _fetch_bootstrap(repo: str, ref: str) -> tuple:
    """Fetch and decode tusk-bootstrap.json. Returns (data_dict, error_str)."""
    url = f"repos/{repo}/contents/tusk-bootstrap.json?ref={ref}"
    try:
        result = subprocess.run(
            ["gh", "api", url, "--jq", ".content"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
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


def _validate_task(task: dict, path: str) -> str | None:
    if not isinstance(task, dict):
        return f"{path} is not an object"
    missing = REQUIRED_TASK_FIELDS - task.keys()
    if missing:
        return f"{path} missing required fields: {sorted(missing)}"
    criteria = task.get("criteria")
    if not isinstance(criteria, list) or len(criteria) == 0:
        return f"{path}.criteria must be a non-empty array"
    if any(not isinstance(c, str) for c in criteria):
        return f"{path}.criteria must be an array of strings"
    migration_hints = task.get("migration_hints")
    if migration_hints is not None and (
        not isinstance(migration_hints, list)
        or any(not isinstance(h, str) for h in migration_hints)
    ):
        return f"{path}.migration_hints must be an array of strings"
    return None


def _validate_manifest_file(entry: dict, path: str) -> str | None:
    if not isinstance(entry, dict):
        return f"{path} is not an object"
    if "path" not in entry:
        return f"{path} missing required field 'path'"
    path_err = validate_relative_path(entry["path"])
    if path_err:
        return f"{path}.path: {path_err}"
    if "content" not in entry:
        return f"{path} missing required field 'content'"
    if not isinstance(entry["content"], str):
        return f"{path}.content must be a string"
    mode = entry.get("mode", "create_only")
    if mode not in VALID_MANIFEST_MODES:
        valid_list = sorted(VALID_MANIFEST_MODES)
        return f"{path}.mode must be one of {valid_list}"
    if mode == "marker_block":
        if not isinstance(entry.get("begin_marker"), str) or not entry["begin_marker"]:
            return f"{path}.begin_marker must be a non-empty string for marker_block"
        if not isinstance(entry.get("end_marker"), str) or not entry["end_marker"]:
            return f"{path}.end_marker must be a non-empty string for marker_block"
    return None


def _validate_string_array(value, path: str) -> str | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return f"{path} must be an array of strings"
    return None


def _validate_name_claim_array(value, path: str, *, name_key: str, value_key: str) -> str | None:
    if not isinstance(value, list):
        return f"{path} must be an array"
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, dict):
            return f"{item_path} is not an object"
        for key in (name_key, value_key):
            if not isinstance(item.get(key), str) or not item[key].strip():
                return f"{item_path}.{key} must be a non-empty string"
    return None


def _validate_context_atoms(value, path: str) -> str | None:
    if not isinstance(value, list):
        return f"{path} must be an array"
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, dict):
            return f"{item_path} is not an object"
        if item.get("type") not in VALID_CONTEXT_TYPES:
            return f"{item_path}.type must be one of {sorted(VALID_CONTEXT_TYPES)}"
        if not isinstance(item.get("content"), str) or not item["content"].strip():
            return f"{item_path}.content must be a non-empty string"
    return None


def _validate_module(module: dict, path: str) -> str | None:
    if not isinstance(module, dict):
        return f"{path} is not an object"
    missing = REQUIRED_MODULE_FIELDS - module.keys()
    if missing:
        return f"{path} missing required fields: {sorted(missing)}"
    for key in REQUIRED_MODULE_FIELDS:
        if not isinstance(module.get(key), str) or not module[key].strip():
            return f"{path}.{key} must be a non-empty string"

    applicability = module.get("applicability", {})
    if not isinstance(applicability, dict):
        return f"{path}.applicability must be an object"
    unknown = set(applicability) - VALID_APPLICABILITY_KEYS
    if unknown:
        return f"{path}.applicability has unknown keys: {sorted(unknown)}"
    for key, value in applicability.items():
        err = _validate_string_array(value, f"{path}.applicability.{key}")
        if err:
            return err

    for field in ("files", "optional_files", "append_operations"):
        entries = module.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return f"{path}.{field} must be an array"
        for i, entry in enumerate(entries):
            err = _validate_manifest_file(entry, f"{path}.{field}[{i}]")
            if err:
                return err

    for field in ("dependencies", "verification_hints"):
        if field in module:
            err = _validate_string_array(module[field], f"{path}.{field}")
            if err:
                return err

    if "pillars" in module:
        err = _validate_name_claim_array(module["pillars"], f"{path}.pillars", name_key="name", value_key="claim")
        if err:
            return err

    if "glossary" in module:
        err = _validate_name_claim_array(module["glossary"], f"{path}.glossary", name_key="term", value_key="definition")
        if err:
            return err

    if "context_atoms" in module:
        err = _validate_context_atoms(module["context_atoms"], f"{path}.context_atoms")
        if err:
            return err

    tasks = module.get("tasks")
    if tasks is not None:
        if not isinstance(tasks, list):
            return f"{path}.tasks must be an array"
        for i, task in enumerate(tasks):
            err = _validate_task(task, f"{path}.tasks[{i}]")
            if err:
                return err

    return None


def _validate(data: dict) -> str | None:
    """Validate required keys. Returns error string or None."""
    if not isinstance(data, dict):
        return "bootstrap file is not a JSON object"

    missing_top = REQUIRED_TOP_LEVEL - data.keys()
    if missing_top:
        return f"missing required keys: {sorted(missing_top)}"
    manifest_schema_version = data.get("manifest_schema_version")
    if manifest_schema_version is not None and not isinstance(manifest_schema_version, int):
        return "manifest_schema_version must be an integer"

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return "tasks must be an array"

    for i, task in enumerate(tasks):
        err = _validate_task(task, f"tasks[{i}]")
        if err:
            return err

    manifest_files = data.get("manifest_files")
    if manifest_files is not None:
        if not isinstance(manifest_files, list):
            return "manifest_files must be an array"
        for i, entry in enumerate(manifest_files):
            err = _validate_manifest_file(entry, f"manifest_files[{i}]")
            if err:
                return err

    modules = data.get("modules")
    if modules is not None:
        if not isinstance(modules, list):
            return "modules must be an array"
        for i, module in enumerate(modules):
            err = _validate_module(module, f"modules[{i}]")
            if err:
                return err

    return None


def _empty_lib(name: str, repo: str, error: str) -> dict:
    return {
        "name": name,
        "repo": repo,
        "tasks": [],
        "modules": [],
        "manifest_files": [],
        "manifest_schema_version": 1,
        "error": error,
    }


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
        print(dumps({"libs": []}))
        return

    libs_out = []

    for name, lib_cfg in project_libs.items():
        repo = lib_cfg.get("repo", "")
        ref = lib_cfg.get("ref", "main")

        if not repo:
            libs_out.append(_empty_lib(name, repo, "missing repo in config"))
            continue

        data, fetch_err = _fetch_bootstrap(repo, ref)
        if fetch_err:
            libs_out.append(_empty_lib(name, repo, fetch_err))
            continue

        val_err = _validate(data)
        if val_err:
            libs_out.append(_empty_lib(name, repo, f"invalid bootstrap: {val_err}"))
            continue

        libs_out.append({
            "name": name,
            "repo": repo,
            "tasks": data["tasks"],
            "modules": data.get("modules") or [],
            "manifest_files": data.get("manifest_files") or [],
            "manifest_schema_version": data.get("manifest_schema_version") or data.get("version") or 1,
            "error": None,
        })

    print(dumps({"libs": libs_out}))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-fetch-bootstrap", file=sys.stderr)
        sys.exit(1)
    main()
