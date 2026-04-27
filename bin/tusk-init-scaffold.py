#!/usr/bin/env python3
"""Scaffold fresh-project directories with .gitkeep + per-directory routing stubs.

Companion CLI for the fresh-project path of /tusk-init. Given a JSON spec of
directories (each with a name, purpose, and assigned agent), creates each
directory plus a `.gitkeep` and an install-mode-aware routing stub
(`CLAUDE.md` in Claude installs, `AGENTS.md` in Codex installs). Existing
directories with files in them are skipped — tusk does not overwrite user code.

Usage:
    tusk init-scaffold --spec '<json>' [--mode claude|codex] [--repo-root <path>]

Spec format:
    [
      {"name": "ios", "purpose": "iOS app sources (Swift, UIKit, SwiftUI).", "agent": "mobile"},
      {"name": "backend", "purpose": "API and service code.",                "agent": "backend"}
    ]

Options:
    --spec <json>        REQUIRED. JSON array of {name, purpose, agent} objects.
    --mode <claude|codex> Stub format. Auto-detected from .claude/ or AGENTS.md
                          presence at <repo-root> if omitted.
    --repo-root <path>   Defaults to $PWD. Directories are created under this
                          path; mode auto-detection inspects this path.

Output (JSON):
    {
      "success": true,
      "mode": "claude" | "codex",
      "repo_root": "/abs/path",
      "created": [
        {"directory": "ios", "stub": "ios/CLAUDE.md", "files": [".gitkeep", "CLAUDE.md"]}
      ],
      "skipped": [
        {"directory": "src", "reason": "directory already contains files"}
      ]
    }
    {"success": false, "error": "<reason>"}
"""

import argparse
import json
import os
import re
import sys


def _emit(payload: dict, exit_code: int = 0) -> None:
    print(json.dumps(payload))
    sys.exit(exit_code)


def _detect_mode(repo_root: str) -> str:
    """Auto-detect install mode from repo layout. Mirrors install.sh's logic."""
    if os.path.isdir(os.path.join(repo_root, ".claude")):
        return "claude"
    if os.path.isfile(os.path.join(repo_root, "AGENTS.md")):
        return "codex"
    # Default to claude — matches install.sh's "no .claude/ and no AGENTS.md"
    # error path; here we still want a usable default for fresh-fresh projects.
    return "claude"


def _validate_dir_name(name: str) -> str:
    """Reject path traversal and absolute paths. Returns a clean relative path."""
    if not name or not name.strip():
        return ""
    name = name.strip()
    if os.path.isabs(name):
        return ""
    if ".." in name.split("/") or ".." in name.split(os.sep):
        return ""
    if not re.match(r"^[a-zA-Z0-9._/-]+$", name):
        return ""
    return name.rstrip("/")


def _has_real_content(dir_path: str) -> bool:
    """True when the directory exists and contains any entry other than .gitkeep."""
    if not os.path.isdir(dir_path):
        return False
    for entry in os.listdir(dir_path):
        if entry != ".gitkeep":
            return True
    return False


def _stub_body(dir_name: str, purpose: str, agent: str) -> str:
    """Render the routing-context stub. Same body for both Claude and Codex modes —
    only the filename differs (CLAUDE.md vs AGENTS.md)."""
    purpose = (purpose or "").strip() or "(no description provided)"
    agent = (agent or "").strip()
    agent_line = f"**Assigned agent:** `{agent}`\n\n" if agent else ""
    return (
        f"# `{dir_name}/`\n"
        f"\n"
        f"{purpose}\n"
        f"\n"
        f"{agent_line}"
        f"This directory was scaffolded by `/tusk-init`. Per-directory `CLAUDE.md` / "
        f"`AGENTS.md` files give agents routing context — what the directory is for "
        f"and which agent owns the work.\n"
    )


def _scaffold_one(repo_root: str, mode: str, entry: dict) -> dict:
    """Create one directory with .gitkeep + routing stub. Returns a dict with
    either {"created": {...}} or {"skipped": {...}}."""
    raw_name = entry.get("name", "")
    name = _validate_dir_name(raw_name)
    if not name:
        return {"skipped": {"directory": raw_name, "reason": "invalid directory name"}}

    abs_dir = os.path.join(repo_root, name)
    if _has_real_content(abs_dir):
        return {"skipped": {"directory": name, "reason": "directory already contains files"}}

    os.makedirs(abs_dir, exist_ok=True)

    files_written: list = []
    gitkeep_path = os.path.join(abs_dir, ".gitkeep")
    if not os.path.exists(gitkeep_path):
        with open(gitkeep_path, "w", encoding="utf-8") as f:
            f.write("")
        files_written.append(".gitkeep")

    stub_filename = "CLAUDE.md" if mode == "claude" else "AGENTS.md"
    stub_path = os.path.join(abs_dir, stub_filename)
    if not os.path.exists(stub_path):
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(_stub_body(name, entry.get("purpose", ""), entry.get("agent", "")))
        files_written.append(stub_filename)

    return {
        "created": {
            "directory": name,
            "stub": f"{name}/{stub_filename}",
            "files": files_written,
        }
    }


def main():
    if len(sys.argv) < 3:
        _emit(
            {"success": False, "error": "tusk-init-scaffold.py requires <db_path> and <config_path>"},
            exit_code=1,
        )

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--mode", choices=["claude", "codex"], default=None)
    parser.add_argument("--repo-root", dest="repo_root", default=None)
    args, _ = parser.parse_known_args(sys.argv[3:])

    repo_root = os.path.abspath(args.repo_root or os.environ.get("TUSK_REPO_ROOT") or os.getcwd())
    if not os.path.isdir(repo_root):
        _emit({"success": False, "error": f"repo-root does not exist: {repo_root}"}, exit_code=1)

    try:
        spec = json.loads(args.spec)
    except json.JSONDecodeError as e:
        _emit({"success": False, "error": f"--spec is not valid JSON: {e}"}, exit_code=1)

    if not isinstance(spec, list):
        _emit({"success": False, "error": "--spec must be a JSON array"}, exit_code=1)

    mode = args.mode or _detect_mode(repo_root)

    created: list = []
    skipped: list = []
    for entry in spec:
        if not isinstance(entry, dict):
            skipped.append({"directory": str(entry), "reason": "spec entry is not an object"})
            continue
        result = _scaffold_one(repo_root, mode, entry)
        if "created" in result:
            created.append(result["created"])
        else:
            skipped.append(result["skipped"])

    _emit({
        "success": True,
        "mode": mode,
        "repo_root": repo_root,
        "created": created,
        "skipped": skipped,
    })


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-scaffold --spec '<json>' [--mode claude|codex]", file=sys.stderr)
        sys.exit(1)
    main()
