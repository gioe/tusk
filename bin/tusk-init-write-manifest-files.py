#!/usr/bin/env python3
"""Write tusk-bootstrap.json `manifest_files` entries into the project tree.

Companion CLI for /tusk-init Step 8.5 and `tusk add-lib`. Both call this
script after a user accepts a lib's bootstrap so the deterministic "add the
dep" file edits land without an extra agent round-trip.

Three modes are supported per entry:

  - create_only (default) — write the file only if it does not already exist
  - append_if_missing     — append `content` to the file iff `content` is not
                            already a substring of the file (idempotent line
                            append for files like requirements.txt)
  - marker_block          — create or replace only the bounded section between
                            `begin_marker` and `end_marker`

Existing files are never overwritten. Re-running against an unchanged tree
is a no-op.

Usage:
    tusk init-write-manifest-files (--spec '<json>' | --spec-file <path>) [--repo-root <path>] [--dry-run] [--intent-file <path>]

`--spec` accepts the JSON spec on argv (convenient for small bootstraps).
`--spec-file` reads the same JSON from a file — use this when the content is
large enough to risk the platform's ARG_MAX limit (~256KB on macOS, ~2MB on
Linux). The two flags are mutually exclusive.

Spec format (matches the manifest_files block from tusk-bootstrap.json):
    [
      {"path": "Package.swift",      "content": "// swift\n"},
      {"path": "requirements.txt",   "content": "gioe-libs\n", "mode": "append_if_missing"},
      {"path": "Package.swift",      "content": ".package(...)\n", "mode": "marker_block", "begin_marker": "// BEGIN TUSK", "end_marker": "// END TUSK"}
    ]

Output (JSON):
    {
      "success": true,
      "repo_root": "/abs/path",
      "wrote":   [{"path": "Package.swift",    "mode": "create_only"}],
      "skipped": [{"path": "requirements.txt", "mode": "append_if_missing", "reason": "content already present"}],
      "conflicts": [],
      "summary": "wrote 1 file, skipped 1 existing"
    }
    {"success": false, "error": "<reason>"}
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-path-lib.py and tusk-json-lib.py

_path_lib = tusk_loader.load("tusk-path-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
validate_relative_path = _path_lib.validate_relative_path
dumps = _json_lib.dumps


VALID_MODES = {"create_only", "append_if_missing", "marker_block"}
TEMPLATE_RE = re.compile(r"{{\s*([A-Za-z0-9_.-]+)\s*}}")


def _emit(payload: dict, exit_code: int = 0) -> None:
    print(dumps(payload))
    sys.exit(exit_code)


def _summary(wrote: list, skipped: list) -> str:
    n_wrote = len(wrote)
    n_skipped = len(skipped)
    wrote_word = "file" if n_wrote == 1 else "files"
    return f"wrote {n_wrote} {wrote_word}, skipped {n_skipped} existing"


def _lookup_template_value(context: dict, key: str):
    value = context
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _render_template(content: str, intent: dict | None) -> tuple[str | None, str | None]:
    if intent is None:
        return content, None

    def replace(match):
        key = match.group(1)
        value = _lookup_template_value(intent, key)
        if value is None:
            raise KeyError(key)
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return str(value)

    try:
        return TEMPLATE_RE.sub(replace, content), None
    except KeyError as e:
        return None, f"missing template variable: {e.args[0]}"


def _dry_run_wrote(rel_path: str, mode: str) -> dict:
    return {"path": rel_path, "mode": mode, "dry_run": True}


def _marker_block(content: str, begin_marker: str, end_marker: str) -> str:
    suffix = "" if content.endswith("\n") else "\n"
    return f"{begin_marker}\n{content}{suffix}{end_marker}"


def _replace_marker_block(existing: str, content: str, begin_marker: str, end_marker: str) -> tuple[str | None, str | None]:
    begin_count = existing.count(begin_marker)
    end_count = existing.count(end_marker)
    if begin_count != 1 or end_count != 1:
        return None, "marker_block requires exactly one begin marker and one end marker"

    begin_idx = existing.find(begin_marker)
    end_idx = existing.find(end_marker)
    if end_idx < begin_idx:
        return None, "marker_block end marker appears before begin marker"

    replacement = _marker_block(content, begin_marker, end_marker)
    end_after = end_idx + len(end_marker)
    return existing[:begin_idx] + replacement + existing[end_after:], None


def _write_one(repo_root: str, entry: dict, *, dry_run: bool = False, intent: dict | None = None) -> dict:
    """Apply one manifest_files entry. Returns wrote, skipped, conflict, or error."""
    if not isinstance(entry, dict):
        return {"error": "entry is not an object"}

    raw_path = entry.get("path")
    path_err = validate_relative_path(raw_path)
    if path_err:
        return {"error": f"path: {path_err}"}

    if "content" not in entry:
        return {"error": "entry missing required field 'content'"}
    content = entry["content"]
    if not isinstance(content, str):
        return {"error": "content must be a string"}
    rendered, render_error = _render_template(content, intent)
    if render_error:
        raw_path = raw_path.strip() if isinstance(raw_path, str) else raw_path
        return {"conflict": {"path": raw_path, "mode": entry.get("mode", "create_only"), "reason": render_error}}
    content = rendered

    mode = entry.get("mode", "create_only")
    if mode not in VALID_MODES:
        return {"error": f"mode must be one of {sorted(VALID_MODES)}"}
    if mode == "marker_block":
        begin_marker = entry.get("begin_marker")
        end_marker = entry.get("end_marker")
        if not isinstance(begin_marker, str) or not begin_marker:
            return {"error": "marker_block requires non-empty string field 'begin_marker'"}
        if not isinstance(end_marker, str) or not end_marker:
            return {"error": "marker_block requires non-empty string field 'end_marker'"}

    rel_path = raw_path.strip()
    abs_path = os.path.join(repo_root, rel_path)
    abs_dir = os.path.dirname(abs_path)

    if mode == "create_only":
        if os.path.exists(abs_path):
            return {"skipped": {"path": rel_path, "mode": mode, "reason": "already exists"}}
        if dry_run:
            return {"wrote": _dry_run_wrote(rel_path, mode)}
        if abs_dir:
            os.makedirs(abs_dir, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"wrote": {"path": rel_path, "mode": mode}}

    if mode == "append_if_missing":
        if os.path.isfile(abs_path):
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    existing = f.read()
            except (OSError, UnicodeDecodeError) as e:
                return {"error": f"failed to read {rel_path}: {e}"}
            if content in existing:
                return {"skipped": {"path": rel_path, "mode": mode, "reason": "content already present"}}
            if dry_run:
                return {"wrote": _dry_run_wrote(rel_path, mode)}
            prefix = "" if existing.endswith("\n") or existing == "" else "\n"
            with open(abs_path, "a", encoding="utf-8") as f:
                f.write(prefix + content)
            return {"wrote": {"path": rel_path, "mode": mode}}

        # File doesn't exist yet — append-mode falls back to creating it.
        if dry_run:
            return {"wrote": _dry_run_wrote(rel_path, mode)}
        if abs_dir:
            os.makedirs(abs_dir, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"wrote": {"path": rel_path, "mode": mode}}

    # marker_block
    begin_marker = entry["begin_marker"]
    end_marker = entry["end_marker"]
    if os.path.isfile(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return {"error": f"failed to read {rel_path}: {e}"}
        updated, marker_error = _replace_marker_block(existing, content, begin_marker, end_marker)
        if marker_error:
            return {"conflict": {"path": rel_path, "mode": mode, "reason": marker_error}}
        if updated == existing:
            return {"skipped": {"path": rel_path, "mode": mode, "reason": "marker block already current"}}
        if dry_run:
            return {"wrote": _dry_run_wrote(rel_path, mode)}
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(updated)
        return {"wrote": {"path": rel_path, "mode": mode}}

    block = _marker_block(content, begin_marker, end_marker) + "\n"
    if dry_run:
        return {"wrote": _dry_run_wrote(rel_path, mode)}
    if abs_dir:
        os.makedirs(abs_dir, exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(block)
    return {"wrote": {"path": rel_path, "mode": mode}}


def main():
    if len(sys.argv) < 3:
        _emit(
            {"success": False, "error": "tusk-init-write-manifest-files.py requires <db_path> and <config_path>"},
            exit_code=1,
        )

    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    spec_group = parser.add_mutually_exclusive_group(required=True)
    spec_group.add_argument("--spec")
    spec_group.add_argument("--spec-file", dest="spec_file")
    parser.add_argument("--repo-root", dest="repo_root", default=None)
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--intent-file", dest="intent_file", default=None)
    try:
        args, _ = parser.parse_known_args(sys.argv[3:])
    except SystemExit:
        _emit(
            {"success": False, "error": "exactly one of --spec or --spec-file is required"},
            exit_code=1,
        )

    repo_root = os.path.abspath(args.repo_root or os.environ.get("TUSK_REPO_ROOT") or os.getcwd())
    if not os.path.isdir(repo_root):
        _emit({"success": False, "error": f"repo-root does not exist: {repo_root}"}, exit_code=1)

    if args.spec_file is not None:
        try:
            with open(args.spec_file, "r", encoding="utf-8") as f:
                spec_raw = f.read()
        except OSError as e:
            _emit({"success": False, "error": f"failed to read --spec-file: {e}"}, exit_code=1)
        spec_source = "--spec-file"
    else:
        spec_raw = args.spec
        spec_source = "--spec"

    try:
        spec = json.loads(spec_raw)
    except json.JSONDecodeError as e:
        _emit({"success": False, "error": f"{spec_source} is not valid JSON: {e}"}, exit_code=1)

    if not isinstance(spec, list):
        _emit({"success": False, "error": "--spec must be a JSON array"}, exit_code=1)

    intent = None
    if args.intent_file is not None:
        try:
            with open(args.intent_file, "r", encoding="utf-8") as f:
                intent = json.load(f)
        except OSError as e:
            _emit({"success": False, "error": f"failed to read --intent-file: {e}"}, exit_code=1)
        except json.JSONDecodeError as e:
            _emit({"success": False, "error": f"--intent-file is not valid JSON: {e}"}, exit_code=1)
        if not isinstance(intent, dict):
            _emit({"success": False, "error": "--intent-file must contain a JSON object"}, exit_code=1)

    wrote: list = []
    skipped: list = []
    conflicts: list = []
    for i, entry in enumerate(spec):
        result = _write_one(repo_root, entry, dry_run=args.dry_run, intent=intent)
        if "error" in result:
            _emit({"success": False, "error": f"manifest_files[{i}]: {result['error']}"}, exit_code=1)
        if "wrote" in result:
            wrote.append(result["wrote"])
        elif "skipped" in result:
            skipped.append(result["skipped"])
        else:
            conflict = result["conflict"]
            conflict["index"] = i
            conflicts.append(conflict)

    if conflicts:
        _emit({
            "success": False,
            "repo_root": repo_root,
            "wrote": wrote,
            "skipped": skipped,
            "conflicts": conflicts,
            "summary": _summary(wrote, skipped),
        }, exit_code=1)

    _emit({
        "success": True,
        "repo_root": repo_root,
        "wrote": wrote,
        "skipped": skipped,
        "conflicts": conflicts,
        "summary": _summary(wrote, skipped),
    })


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(
            "Use: tusk init-write-manifest-files (--spec '<json>' | --spec-file <path>) "
            "[--repo-root <path>] [--dry-run] [--intent-file <path>]",
            file=sys.stderr,
        )
        sys.exit(1)
    main()
