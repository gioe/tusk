#!/usr/bin/env python3
"""Remove tusk-installed .git/hooks dispatchers and restore any chained user hooks.

Inverse of the dispatcher install in install.sh (TASK-155). For each of
pre-commit, pre-push, commit-msg in $REPO_ROOT/.git/hooks/:

  - If the file carries the TUSK_HOOK_DISPATCHER_V1 marker: remove it.
    If a sibling <event>.pre-tusk file exists, restore it back to <event>
    (rename preserves the executable bit set at install time).
  - If the file exists but lacks the marker: leave it untouched
    (user-authored hook).
  - If the file is absent but a stray <event>.pre-tusk exists from a
    partial uninstall, restore it.

Idempotent: running twice exits 0; the second pass reports "skipped no marker"
(if a chain was restored on the first pass) or "not present" (if none was)
without mutating anything.
"""

import json
import os
import sys

MARKER = "TUSK_HOOK_DISPATCHER_V1"
EVENTS = ("pre-commit", "pre-push", "commit-msg")

_LABELS = {
    "removed": "removed",
    "restored_chain": "removed (restored chained user hook)",
    "restored_chain_only": "restored chained user hook (dispatcher already absent)",
    "skipped_no_marker": "skipped (no tusk marker — user-authored)",
    "not_present": "not present",
}


def _has_marker(path: str) -> bool:
    try:
        with open(path, "r", errors="replace") as f:
            return MARKER in f.read()
    except OSError:
        return False


def uninstall(repo_root: str) -> list:
    hooks_dir = os.path.join(repo_root, ".git", "hooks")
    results = []
    for event in EVENTS:
        target = os.path.join(hooks_dir, event)
        chained = target + ".pre-tusk"
        entry = {"event": event}

        if not os.path.exists(target):
            if os.path.exists(chained):
                os.rename(chained, target)
                entry["status"] = "restored_chain_only"
            else:
                entry["status"] = "not_present"
            results.append(entry)
            continue

        if not _has_marker(target):
            entry["status"] = "skipped_no_marker"
            results.append(entry)
            continue

        os.remove(target)
        if os.path.exists(chained):
            os.rename(chained, target)
            entry["status"] = "restored_chain"
        else:
            entry["status"] = "removed"
        results.append(entry)
    return results


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: tusk-uninstall-hooks.py <repo_root> [--json]", file=sys.stderr)
        return 1
    repo_root = sys.argv[1]
    as_json = "--json" in sys.argv[2:]

    hooks_dir = os.path.join(repo_root, ".git", "hooks")
    if not os.path.isdir(hooks_dir):
        msg = f"  Warning: {hooks_dir} not found — nothing to uninstall"
        if as_json:
            print(json.dumps({"hooks_dir": hooks_dir, "results": [], "warning": "hooks_dir_missing"}))
        else:
            print(msg)
        return 0

    results = uninstall(repo_root)

    if as_json:
        print(json.dumps({"hooks_dir": hooks_dir, "results": results}))
    else:
        for r in results:
            print(f"  .git/hooks/{r['event']}: {_LABELS[r['status']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
