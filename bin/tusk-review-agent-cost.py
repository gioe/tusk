#!/usr/bin/env python3
"""Aggregate cost from spawned reviewer agent transcripts.

Discovers Claude Code subagent JSONL transcripts that were created or
modified during a /review-commits agent-path review and sums their cost
and token usage. The orchestrator's own JSONL (which records only wait
time, not the agent's API spend) is explicitly excluded.

Called by the tusk wrapper:
    tusk review-agent-cost --since <epoch> [--exclude-jsonl <path>]
                           [--project-dir <path>]
    tusk review-agent-cost --print-orchestrator-jsonl

The first form aggregates cost. The second prints the path of the
orchestrator's most-recent JSONL — call it before spawning the agent
to capture the value passed to `--exclude-jsonl` after completion.

Outputs (compact JSON unless --pretty / TUSK_PRETTY=1):
    {
      "cost_dollars": float,
      "tokens_in":    int,
      "tokens_out":   int,
      "request_count": int,
      "transcripts":   [<path>, ...]
    }

Exit codes:
    0  success — aggregation succeeded; JSON printed
    1  no candidate JSONLs found (orchestrator should fall back to
       backfill-cost auto-compute or leave the row's cost as-is)
    2  invalid arguments / project dir does not exist / discovery mode
       could not locate the orchestrator's transcript
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-pricing-lib.py and tusk-json-lib.py
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
pretty_requested = _json_lib.pretty_requested


def _load_pricing_lib():
    lib = tusk_loader.load("tusk-pricing-lib")
    lib.load_pricing()
    return lib


def _candidate_jsonls(claude_dir: Path, since_epoch: float, exclude_realpath: str | None) -> list[Path]:
    """Return JSONLs in *claude_dir* with mtime >= since_epoch, minus the excluded path.

    Subagent sessions write a distinct `<session-uuid>.jsonl` into the
    same project hash dir as the orchestrator. Filtering by mtime
    eliminates older transcripts; excluding the orchestrator's own path
    eliminates its continuously-updated wait-time JSONL, leaving only
    the agent transcripts spawned during the review window.
    """
    out: list[Path] = []
    for jsonl in claude_dir.glob("*.jsonl"):
        if exclude_realpath is not None and os.path.realpath(str(jsonl)) == exclude_realpath:
            continue
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < since_epoch:
            continue
        out.append(jsonl)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def aggregate_agent_cost(
    since_epoch: float,
    exclude_jsonl: str | None,
    project_dir: str | None,
) -> dict:
    """Aggregate cost across agent JSONLs created during the review window.

    Returns a dict with cost_dollars/tokens_in/tokens_out/request_count
    and the list of transcripts that contributed. `request_count == 0`
    means no candidate transcripts produced API requests in the window;
    callers should treat that as "no agent cost discoverable" and fall
    back to whatever was already on the row.
    """
    lib = _load_pricing_lib()
    project_dir = project_dir or os.getcwd()
    project_hash = lib.derive_project_hash(project_dir)
    claude_dir = Path.home() / ".claude" / "projects" / project_hash

    aggregated = {
        "cost_dollars": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "request_count": 0,
        "transcripts": [],
    }
    if not claude_dir.exists():
        return aggregated

    exclude_real = os.path.realpath(exclude_jsonl) if exclude_jsonl else None
    candidates = _candidate_jsonls(claude_dir, since_epoch, exclude_real)
    if not candidates:
        return aggregated

    started_at = datetime.fromtimestamp(since_epoch, tz=timezone.utc)
    for cand in candidates:
        totals = lib.aggregate_session(str(cand), started_at, None)
        if totals.get("request_count", 0) == 0:
            continue
        aggregated["cost_dollars"] += lib.compute_cost(totals)
        aggregated["tokens_in"] += lib.compute_tokens_in(totals)
        aggregated["tokens_out"] += totals.get("output_tokens", 0)
        aggregated["request_count"] += totals.get("request_count", 0)
        aggregated["transcripts"].append(str(cand))

    return aggregated


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk review-agent-cost",
        description="Aggregate cost from spawned reviewer agent transcripts.",
    )
    parser.add_argument(
        "--since",
        type=float,
        default=None,
        help="Earliest mtime (epoch seconds) for candidate JSONLs — typically the agent spawn time.",
    )
    parser.add_argument(
        "--exclude-jsonl",
        dest="exclude_jsonl",
        default=None,
        help="Orchestrator's own JSONL path (excluded from aggregation).",
    )
    parser.add_argument(
        "--project-dir",
        dest="project_dir",
        default=None,
        help="Override the project dir used to derive the Claude transcripts hash (default: cwd).",
    )
    parser.add_argument(
        "--print-orchestrator-jsonl",
        dest="print_orchestrator_jsonl",
        action="store_true",
        help="Print the orchestrator's most-recent JSONL path and exit. Use before spawning the agent.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (also via TUSK_PRETTY=1).",
    )
    args = parser.parse_args(argv)

    project_dir = args.project_dir or os.getcwd()
    if not os.path.isdir(project_dir):
        print(f"Error: project dir not found: {project_dir}", file=sys.stderr)
        return 2

    if args.print_orchestrator_jsonl:
        lib = _load_pricing_lib()
        path = lib.find_transcript()
        if not path:
            print("Error: no transcript found for current project", file=sys.stderr)
            return 2
        print(path)
        return 0

    if args.since is None:
        print("Error: --since is required (omit only with --print-orchestrator-jsonl).", file=sys.stderr)
        return 2

    result = aggregate_agent_cost(
        since_epoch=args.since,
        exclude_jsonl=args.exclude_jsonl,
        project_dir=project_dir,
    )

    print(dumps(result, pretty=args.pretty or pretty_requested()))

    return 0 if result["request_count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
