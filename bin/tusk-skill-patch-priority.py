#!/usr/bin/env python3
"""Compute (and optionally apply) a priority for a skill-patch follow-up task
derived from its retro-signals, so these tasks no longer land at the unmodified
default priority and rot in the backlog (TASK-715).

Called by the tusk wrapper:
    tusk skill-patch-priority <task_id> [--apply] [--format json|text]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)
    remaining   — flags

Signal → priority model
-----------------------
The priority is computed from a single integer "pressure" score built from the
retro-signals that indicate a finding keeps biting:

    pressure = reopen_count
             + len(rework_chain.fixes)
             + len(rework_chain.fixed_by)
             + sum(theme.count for theme in review_themes)

Higher reopen counts and longer rework chains (in either direction of the
fixes_task_id FK) yield a higher pressure, and review themes that recur across
passes add to it. The pressure is then mapped onto the project's ordered
`priorities` list (config.default.json, highest first). A task with no pressure
signals lands at the configured *default* priority (NOT the highest), so that
skill-patch tasks that genuinely carry no rework history are not artificially
inflated — but any non-zero pressure lifts the priority above default and the
score is monotonic in the pressure inputs.

Exit codes:
    0 — success
    1 — error (bad arguments, task not found, DB issue)
"""

import argparse
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-retro-signals.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_signals_mod = tusk_loader.load("tusk-retro-signals")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
build_signals = _signals_mod.build_signals

# Fallback priority ladder (highest first) used only when config has no
# non-empty `priorities` array. Mirrors config.default.json's default ladder.
DEFAULT_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]


def _resolve_task_id(raw: str) -> int:
    """Accept '5' or 'TASK-5' → 5. Raises ValueError on junk."""
    return int(re.sub(r"^TASK-", "", raw, flags=re.IGNORECASE))


def load_priorities(config_path: str) -> list:
    """Return the ordered `priorities` ladder (highest first) from config,
    falling back to DEFAULT_PRIORITIES when the config is missing/empty."""
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return list(DEFAULT_PRIORITIES)
    priorities = config.get("priorities")
    if isinstance(priorities, list) and priorities:
        return list(priorities)
    return list(DEFAULT_PRIORITIES)


def compute_pressure(signals: dict) -> int:
    """Aggregate the rework-pressure score from retro-signals.

    Sums reopen_count, both directions of the rework chain, and the recurrence
    counts of review themes. Each input contributes additively, so the result is
    monotonic non-decreasing in every input (criterion 3341)."""
    reopen = int(signals.get("reopen_count") or 0)

    rework = signals.get("rework_chain") or {}
    fixes = rework.get("fixes") or []
    fixed_by = rework.get("fixed_by") or []
    rework_count = len(fixes) + len(fixed_by)

    themes = signals.get("review_themes") or []
    theme_count = 0
    for t in themes:
        try:
            theme_count += int(t.get("count") or 0)
        except (AttributeError, TypeError, ValueError):
            theme_count += 0

    return reopen + rework_count + theme_count


def pressure_to_priority(pressure: int, priorities: list) -> str:
    """Map a non-negative pressure score onto the ordered priority ladder.

    `priorities` is highest-first (index 0 = highest). With pressure 0 the task
    lands at the configured *default* priority (the middle of the ladder), and
    each additional unit of pressure steps one rung toward the highest priority,
    saturating at index 0. The mapping is monotonic non-decreasing in pressure:
    more pressure never yields a lower priority."""
    if not priorities:
        priorities = list(DEFAULT_PRIORITIES)
    n = len(priorities)
    # Default rung = middle of the ladder (e.g. "Medium" in the 5-rung default).
    default_index = n // 2
    # Each unit of pressure climbs one rung toward index 0 (highest).
    index = default_index - max(0, pressure)
    if index < 0:
        index = 0
    return priorities[index]


def compute_priority(signals: dict, priorities: list) -> str:
    """End-to-end: retro-signals dict → priority label string."""
    return pressure_to_priority(compute_pressure(signals), priorities)


def apply_priority(conn: sqlite3.Connection, task_id: int, priority: str) -> None:
    """Persist the computed priority on the task row."""
    conn.execute(
        "UPDATE tasks SET priority = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (priority, task_id),
    )
    conn.commit()


def main(argv: list) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk skill-patch-priority",
        description="Compute (and optionally apply) a retro-signal-derived "
        "priority for a skill-patch follow-up task.",
    )
    parser.add_argument("task_id", help="Task ID (integer or TASK-NNN prefix form)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the computed priority onto the task (default: print only).",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv[2:])

    try:
        task_id = _resolve_task_id(args.task_id)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    priorities = load_priorities(config_path)

    try:
        conn = get_connection(db_path)
    except Exception as e:
        print(
            f"tusk skill-patch-priority: failed to open database '{db_path}': "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1
    try:
        row = conn.execute(
            "SELECT priority FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1
        previous_priority = row["priority"]

        try:
            signals = build_signals(conn, task_id)
        except Exception as e:
            print(
                f"tusk skill-patch-priority: failed to collect signals for "
                f"task {task_id}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return 1

        pressure = compute_pressure(signals)
        priority = compute_priority(signals, priorities)

        applied = False
        if args.apply and priority != previous_priority:
            apply_priority(conn, task_id, priority)
            applied = True

        payload = {
            "task_id": task_id,
            "previous_priority": previous_priority,
            "priority": priority,
            "pressure": pressure,
            "applied": applied,
            "reopen_count": int(signals.get("reopen_count") or 0),
            "rework_count": len((signals.get("rework_chain") or {}).get("fixes") or [])
            + len((signals.get("rework_chain") or {}).get("fixed_by") or []),
            "review_theme_count": sum(
                int(t.get("count") or 0)
                for t in (signals.get("review_themes") or [])
            ),
        }

        if args.format == "text":
            verb = "applied" if applied else (
                "unchanged" if priority == previous_priority else "computed"
            )
            print(
                f"TASK-{task_id}: {previous_priority} -> {priority} "
                f"(pressure={pressure}, {verb})"
            )
        else:
            print(dumps(payload))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(
            "Use: tusk skill-patch-priority <task_id> [--apply] [--format json|text]",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
