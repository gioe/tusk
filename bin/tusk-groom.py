#!/usr/bin/env python3
"""Mechanical orchestrator for the /groom-backlog pipeline.

Called by the tusk wrapper:
    tusk groom [--dry-run]

Runs the deterministic, non-LLM pieces of /groom-backlog in sequence and
emits a single JSON document so a human (or a Codex prompt port) can review
the state of the backlog without juggling multiple subcommands.

Pipeline:
  1. autoclose            — expired-deferred / moot-contingent closures
                            (--dry-run: query the same conditions without
                             closing rows)
  2. backlog-scan         — duplicates / unassigned / unsized / expired
                            (calls `tusk backlog-scan` with all four flags)
  3. lint (advisory)      — captures the convention-lint summary

Output JSON shape:
    {
        "dry_run":    bool,
        "expired":    [ {id, summary, expires_at}, ... ],
        "duplicates": [ {task_a, task_b, similarity}, ... ],
        "unassigned": [ {id, summary, domain}, ... ],
        "unsized":    [ {id, summary, domain, task_type}, ... ],
        "autoclose_candidates": {
            "applied":          bool,                    # false in --dry-run
            "expired_deferred": {"count": N, "task_ids": [...]},
            "moot_contingent":  {"count": N, "task_ids": [...]},
            "total":            N
        },
        "lint": {"exit_code": N, "summary": "<last summary line>"}
    }

The semantic-deduplication, code verification, bulk re-prioritization, and
user-confirmation steps of /groom-backlog still require an LLM and are
intentionally out of scope here.

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — flags (--dry-run, --help)
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps

USAGE = "Usage: tusk groom [--dry-run]"

HELP_TEXT = """\
Usage: tusk groom [--dry-run]

Mechanical orchestrator for the /groom-backlog pipeline. Runs autoclose,
backlog-scan (duplicates / unassigned / unsized / expired), and lint in
sequence and prints a single JSON document summarizing the state of the
backlog.

Flags:
  --dry-run   Skip the autoclose UPDATE; report what *would* be closed
              under autoclose_candidates with applied=false. The
              backlog-scan and lint steps run unchanged either way.
  --help      Show this message.

Output JSON keys:
  expired              Open tasks past their expires_at date
  duplicates           Heuristic duplicate pairs among open tasks
  unassigned           To Do tasks with no assignee
  unsized              To Do tasks with no complexity estimate
  autoclose_candidates {expired_deferred, moot_contingent, total, applied}
  lint                 {exit_code, summary} from `tusk lint --quiet`
"""


def _tusk_bin() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")


def run_autoclose(dry_run: bool = False) -> dict:
    """Invoke `tusk autoclose [--dry-run]` and shape its output for this orchestrator.

    Both branches go through the same `tusk autoclose` subprocess so the
    SELECT/UPDATE pair lives in exactly one place — `bin/tusk-autoclose.py`.
    """
    cmd = [_tusk_bin(), "autoclose"]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        # Surface failure rather than producing a misleading summary.
        msg = (result.stderr or result.stdout or "tusk autoclose failed").strip()
        raise RuntimeError(f"tusk autoclose failed (exit {result.returncode}): {msg}")
    payload = json.loads(result.stdout)
    return {
        "applied": payload.get("applied", not dry_run),
        "expired_deferred": payload.get(
            "expired_deferred", {"count": 0, "task_ids": []}
        ),
        "moot_contingent": payload.get(
            "moot_contingent", {"count": 0, "task_ids": []}
        ),
        "total": payload.get("total_closed", 0),
    }


def run_backlog_scan() -> dict:
    """Invoke `tusk backlog-scan` with all four category flags."""
    result = subprocess.run(
        [
            _tusk_bin(),
            "backlog-scan",
            "--duplicates",
            "--unassigned",
            "--unsized",
            "--expired",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "tusk backlog-scan failed").strip()
        raise RuntimeError(
            f"tusk backlog-scan failed (exit {result.returncode}): {msg}"
        )
    return json.loads(result.stdout)


def run_lint() -> dict:
    """Invoke `tusk lint --quiet` and capture a one-line summary.

    Lint is advisory in this context: a non-zero exit is reported but does
    not fail the groom run. The summary field is the last non-empty line of
    stdout (the "OK — N rules passed" / "Summary: …" footer).
    """
    result = subprocess.run(
        [_tusk_bin(), "lint", "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    summary = lines[-1] if lines else ""
    return {"exit_code": result.returncode, "summary": summary}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 1

    # argv[0] (db_path) and argv[1] (config_path) are passed by the bash wrapper
    # but consumed only by the subprocess'd tusk subcommands, not here directly.
    flags = argv[2:]

    if "--help" in flags or "-h" in flags:
        print(HELP_TEXT)
        return 0

    known_flags = {"--dry-run"}
    unknown = [f for f in flags if f not in known_flags]
    if unknown:
        print(f"Unknown flags: {' '.join(unknown)}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1

    dry_run = "--dry-run" in flags

    try:
        autoclose_candidates = run_autoclose(dry_run=dry_run)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"groom: {exc}", file=sys.stderr)
        return 2

    try:
        scan = run_backlog_scan()
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"groom: {exc}", file=sys.stderr)
        return 2

    lint_summary = run_lint()

    result = {
        "dry_run": dry_run,
        "expired": scan.get("expired", []),
        "duplicates": scan.get("duplicates", []),
        "unassigned": scan.get("unassigned", []),
        "unsized": scan.get("unsized", []),
        "autoclose_candidates": autoclose_candidates,
        "lint": lint_summary,
    }
    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
