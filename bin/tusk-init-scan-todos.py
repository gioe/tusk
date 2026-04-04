#!/usr/bin/env python3
"""Scan a project root for TODO/FIXME/HACK/XXX comments and return structured JSON.

Returns JSON array:
[
  {
    "file": "src/api/auth.ts",
    "line": 42,
    "text": "Add rate limiting to login endpoint",
    "keyword": "TODO",
    "priority": "Medium",
    "task_type": "feature"
  },
  ...
]

Keyword → priority/task_type mapping:
  FIXME, HACK → priority=High, task_type=bug
  TODO, XXX   → priority=Medium, task_type=feature
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYWORDS = ["TODO", "FIXME", "HACK", "XXX"]

# Keyword → (priority, task_type)
_KEYWORD_MAP = {
    "FIXME": ("High",   "bug"),
    "HACK":  ("High",   "bug"),
    "TODO":  ("Medium", "feature"),
    "XXX":   ("Medium", "feature"),
}

DEFAULT_EXCLUDE_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build",
    "tusk", "__pycache__", ".venv", "target", ".tox",
    ".mypy_cache", ".pytest_cache", "coverage",
}

# Compiled pattern: matches keyword in a comment-marker context.  The keyword
# must appear after a comment delimiter (// # /* *) or at line start, preceded
# only by whitespace and optional comment chars.  This prevents matching "todo"
# in prose like "use the todo list" or identifiers like "TodoWrite".
_PATTERN = re.compile(
    r"^[ \t]*(?://|/\*+|#+|\*+|<!--)?[ \t]*(TODO|FIXME|HACK|XXX)(?![a-zA-Z0-9_])[:\s]*(.+)?",
    re.IGNORECASE,
)

# Developer-tag pattern: "(name):" or "(name-with-dashes):" immediately after
# keyword, indicating an internal team note rather than an actionable TODO.
_DEV_TAG = re.compile(r"^\([\w.-]+\)\s*:")

# Code-fragment pattern: text that looks like a path suffix, closing bracket,
# or other non-natural-language fragment.
_CODE_FRAGMENT = re.compile(
    r"^[)\]}>.,;:!?/\\]+$"           # pure punctuation/brackets
    r"|^/[\w./-]+$"                   # bare path like /types.js
    r"|^[\w./-]*\.[a-z]{1,4}$",      # file extension like types.js
    re.IGNORECASE,
)

# Minimum summary requirements
_MIN_SUMMARY_CHARS = 10
_MIN_SUMMARY_WORDS = 2

# Binary-file sniff: if any of the first 8 KB contains a null byte, skip.
_BINARY_CHUNK = 8192


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(_BINARY_CHUNK)
        return b"\x00" in chunk
    except OSError:
        return True


def _is_in_string_literal(line: str, match_start: int) -> bool:
    """Return True if match_start falls inside a quoted string literal."""
    in_single = False
    in_double = False
    in_backtick = False
    for i, ch in enumerate(line):
        if i == match_start:
            return in_single or in_double or in_backtick
        if ch == "'" and not in_double and not in_backtick:
            in_single = not in_single
        elif ch == '"' and not in_single and not in_backtick:
            in_double = not in_double
        elif ch == '`' and not in_single and not in_double:
            in_backtick = not in_backtick
    return False


def _is_false_positive(line: str, match_start: int, raw_text: str) -> bool:
    """Return True if the match should be filtered out as a false positive."""
    # 1. Inside a string literal
    if _is_in_string_literal(line, match_start):
        return True
    # 2. Developer name tag like (inigo): or (xaa-ga):
    if _DEV_TAG.match(raw_text):
        return True
    # 3. Too short or too few words
    if len(raw_text) < _MIN_SUMMARY_CHARS or len(raw_text.split()) < _MIN_SUMMARY_WORDS:
        return True
    # 4. Looks like a code fragment
    if _CODE_FRAGMENT.match(raw_text):
        return True
    return False


def _scan_file(filepath: str, relpath: str) -> list:
    """Return list of match dicts for a single file."""
    if _is_binary(filepath):
        return []
    results = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                m = _PATTERN.search(line)
                if m:
                    keyword = m.group(1).upper()
                    raw_text = (m.group(2) or "").strip()
                    # Strip leading punctuation/whitespace and trailing comment closers
                    raw_text = re.sub(r"^[:\-–—\s]+", "", raw_text).strip()
                    raw_text = re.sub(r"\s*(?:\*+/|--+>)\s*$", "", raw_text).strip()
                    if not raw_text or _is_false_positive(line, m.start(), raw_text):
                        continue
                    priority, task_type = _KEYWORD_MAP.get(keyword, ("Medium", "feature"))
                    results.append({
                        "file": relpath,
                        "line": lineno,
                        "text": raw_text,
                        "keyword": keyword,
                        "priority": priority,
                        "task_type": task_type,
                    })
    except OSError:
        pass
    return results


def _should_exclude(name: str, exclude_dirs: set) -> bool:
    return name in exclude_dirs or name.startswith(".")


def scan(root: str, extra_excludes: list | None = None) -> list:
    """Walk root and return all TODO/FIXME/HACK/XXX matches."""
    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)
    if extra_excludes:
        exclude_dirs.update(extra_excludes)

    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place (affects os.walk traversal)
        dirnames[:] = [
            d for d in dirnames
            if not _should_exclude(d, exclude_dirs)
        ]

        for filename in sorted(filenames):
            filepath = os.path.join(dirpath, filename)
            relpath = os.path.relpath(filepath, root)
            results.extend(_scan_file(filepath, relpath))

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list) -> int:
    # argv[0] = db_path (unused), argv[1] = config_path (unused), argv[2:] = flags
    args = argv[2:]

    root = os.getcwd()
    extra_excludes: list = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--exclude" and i + 1 < len(args):
            i += 1
            # Comma-separated list of directories, or multiple --exclude flags
            extra_excludes.extend(d.strip() for d in args[i].split(",") if d.strip())
        elif arg.startswith("--exclude="):
            val = arg[len("--exclude="):]
            extra_excludes.extend(d.strip() for d in val.split(",") if d.strip())
        elif not arg.startswith("--"):
            root = arg
        i += 1

    results = scan(root, extra_excludes if extra_excludes else None)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-scan-todos [--exclude dir,...] [root]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
