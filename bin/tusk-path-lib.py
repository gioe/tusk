"""Shared path-safety helpers for tusk-*.py scripts.

Several scripts validate user-controlled relative paths before resolving
them against the repo root. The same checks (no absolute paths, no '..'
segments, conservative character allowlist) need to live in exactly one
place so a future tightening of the rules can't drift between callers.

Currently shared by:
  - tusk-init-fetch-bootstrap.py — validates manifest_files[*].path entries
    in fetched tusk-bootstrap.json payloads before they reach disk.
  - tusk-init-write-manifest-files.py — re-validates the same paths at
    write time as a defence in depth.

Imported via tusk_loader (hyphenated filename requires it):

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _path_lib = tusk_loader.load("tusk-path-lib")
    validate_relative_path = _path_lib.validate_relative_path
"""

import os
import re


_SAFE_CHARS = re.compile(r"^[a-zA-Z0-9._/-]+$")


def validate_relative_path(path) -> str | None:
    """Reject absolute paths, traversal, and unsafe characters.

    Returns an error string when the path is unsafe, otherwise None. The
    error wording is preserved verbatim so existing tests and CLI error
    messages stay byte-for-byte identical.
    """
    if not isinstance(path, str) or not path.strip():
        return "path must be a non-empty string"
    cleaned = path.strip()
    if os.path.isabs(cleaned):
        return "path must be relative, not absolute"
    if ".." in cleaned.split("/") or ".." in cleaned.split(os.sep):
        return "path contains '..' segment"
    if not _SAFE_CHARS.match(cleaned):
        return "path contains invalid characters"
    return None
