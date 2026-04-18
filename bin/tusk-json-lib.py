"""tusk-json-lib — shared JSON output helper for bin/tusk-*.py scripts.

Output contract: CLI stdout is compact single-line JSON by default. Agents
are the primary consumer; human-readable indentation is opt-in.

Pretty mode is requested via either:
  - the TUSK_PRETTY environment variable set to "1", "true", or "yes"
  - the --pretty flag, which the bin/tusk bash dispatcher translates into
    TUSK_PRETTY=1 before invoking any Python script

Usage:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader
    _json_lib = tusk_loader.load("tusk-json-lib")
    dumps = _json_lib.dumps

    print(dumps(result))
"""

import json
import os


_PRETTY_TRUTHY = frozenset({"1", "true", "yes", "on"})


def pretty_requested() -> bool:
    """Return True when TUSK_PRETTY requests indented output."""
    return os.environ.get("TUSK_PRETTY", "").strip().lower() in _PRETTY_TRUTHY


_COMPACT_SEPARATORS = (",", ":")


def dumps(obj, *, pretty: bool | None = None) -> str:
    """Serialize obj as JSON — compact by default; indent=2 when pretty.

    Compact mode uses the tightest separators (no spaces after comma or
    colon) and ensure_ascii=False so non-ASCII characters survive as UTF-8
    bytes rather than expanding into \\uXXXX escapes. The output is
    agent-consumed and every byte counts.
    """
    if pretty is None:
        pretty = pretty_requested()
    if pretty:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    return json.dumps(obj, separators=_COMPACT_SEPARATORS, ensure_ascii=False)
