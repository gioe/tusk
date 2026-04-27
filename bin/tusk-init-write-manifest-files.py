#!/usr/bin/env python3
"""Write tusk-bootstrap.json `manifest_files` entries into the project tree.

Companion CLI for /tusk-init Step 8.5 and `tusk add-lib`. Both call this
script after a user accepts a lib's bootstrap so the deterministic "add the
dep" file edits land without an extra agent round-trip.

Two modes are supported per entry:

  - create_only (default) — write the file only if it does not already exist
  - append_if_missing     — append `content` to the file iff `content` is not
                            already a substring of the file (idempotent line
                            append for files like requirements.txt)

Existing files are never overwritten. Re-running against an unchanged tree
is a no-op.

Usage:
    tusk init-write-manifest-files --spec '<json>' [--repo-root <path>]

Spec format (matches the manifest_files block from tusk-bootstrap.json):
    [
      {"path": "Package.swift",      "content": "// swift\n"},
      {"path": "requirements.txt",   "content": "gioe-libs\n", "mode": "append_if_missing"}
    ]

Output (JSON):
    {
      "success": true,
      "repo_root": "/abs/path",
      "wrote":   [{"path": "Package.swift",    "mode": "create_only"}],
      "skipped": [{"path": "requirements.txt", "mode": "append_if_missing", "reason": "content already present"}],
      "summary": "wrote 1 file, skipped 1 existing"
    }
    {"success": false, "error": "<reason>"}
"""

import argparse
import json
import os
import re
import sys


VALID_MODES = {"create_only", "append_if_missing"}


def _emit(payload: dict, exit_code: int = 0) -> None:
    print(json.dumps(payload))
    sys.exit(exit_code)


def _validate_path(path) -> str | None:
    """Reject absolute paths, traversal, and unsafe characters. Mirrors
    tusk-init-fetch-bootstrap.py:_validate_manifest_path."""
    if not isinstance(path, str) or not path.strip():
        return "path must be a non-empty string"
    cleaned = path.strip()
    if os.path.isabs(cleaned):
        return "path must be relative, not absolute"
    if ".." in cleaned.split("/") or ".." in cleaned.split(os.sep):
        return "path contains '..' segment"
    if not re.match(r"^[a-zA-Z0-9._/-]+$", cleaned):
        return "path contains invalid characters"
    return None


def _summary(wrote: list, skipped: list) -> str:
    n_wrote = len(wrote)
    n_skipped = len(skipped)
    wrote_word = "file" if n_wrote == 1 else "files"
    return f"wrote {n_wrote} {wrote_word}, skipped {n_skipped} existing"


def _write_one(repo_root: str, entry: dict) -> dict:
    """Apply one manifest_files entry. Returns {"wrote": {...}} or
    {"skipped": {...}} or {"error": "<msg>"}."""
    if not isinstance(entry, dict):
        return {"error": "entry is not an object"}

    raw_path = entry.get("path")
    path_err = _validate_path(raw_path)
    if path_err:
        return {"error": f"path: {path_err}"}

    if "content" not in entry:
        return {"error": "entry missing required field 'content'"}
    content = entry["content"]
    if not isinstance(content, str):
        return {"error": "content must be a string"}

    mode = entry.get("mode", "create_only")
    if mode not in VALID_MODES:
        return {"error": f"mode must be one of {sorted(VALID_MODES)}"}

    rel_path = raw_path.strip()
    abs_path = os.path.join(repo_root, rel_path)
    abs_dir = os.path.dirname(abs_path)
    if abs_dir:
        os.makedirs(abs_dir, exist_ok=True)

    if mode == "create_only":
        if os.path.exists(abs_path):
            return {"skipped": {"path": rel_path, "mode": mode, "reason": "already exists"}}
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"wrote": {"path": rel_path, "mode": mode}}

    # append_if_missing
    if os.path.isfile(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return {"error": f"failed to read {rel_path}: {e}"}
        if content in existing:
            return {"skipped": {"path": rel_path, "mode": mode, "reason": "content already present"}}
        prefix = "" if existing.endswith("\n") or existing == "" else "\n"
        with open(abs_path, "a", encoding="utf-8") as f:
            f.write(prefix + content)
        return {"wrote": {"path": rel_path, "mode": mode}}

    # File doesn't exist yet — append-mode falls back to creating it.
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"wrote": {"path": rel_path, "mode": mode}}


def main():
    if len(sys.argv) < 3:
        _emit(
            {"success": False, "error": "tusk-init-write-manifest-files.py requires <db_path> and <config_path>"},
            exit_code=1,
        )

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--spec", required=True)
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

    wrote: list = []
    skipped: list = []
    for i, entry in enumerate(spec):
        result = _write_one(repo_root, entry)
        if "error" in result:
            _emit({"success": False, "error": f"manifest_files[{i}]: {result['error']}"}, exit_code=1)
        if "wrote" in result:
            wrote.append(result["wrote"])
        else:
            skipped.append(result["skipped"])

    _emit({
        "success": True,
        "repo_root": repo_root,
        "wrote": wrote,
        "skipped": skipped,
        "summary": _summary(wrote, skipped),
    })


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-write-manifest-files --spec '<json>' [--repo-root <path>]", file=sys.stderr)
        sys.exit(1)
    main()
