#!/usr/bin/env python3
"""Orchestrate a session retrospective: pre-aggregated retro signals for a
single task PLUS the cross-retro theme rollup, emitted as one JSON blob.

Called by the tusk wrapper:
    tusk retro <task_id> [--window-days N] [--min-recurrence N]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Bundles the same data /retro consumes (retro-signals + retro-themes) into
one document so Codex (and any non-Claude caller) can run a retrospective
with a single subprocess invocation. The signals object preserves
rework_chain and unconsumed_next_steps inline — they are not lifted to the
top level so the embedded shape stays interchangeable with the standalone
retro-signals output.

Output JSON shape:
    {
        "task_id": N,
        "signals": { ... full retro-signals output ... },
        "themes":  { ... full retro-themes output ... }
    }

Exit codes:
    0 — success
    1 — error (bad arguments, task not found, DB issue)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# loads tusk-db-lib.py, tusk-json-lib.py, tusk-retro-signals.py, tusk-retro-themes.py
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_signals_mod = tusk_loader.load("tusk-retro-signals")
_themes_mod = tusk_loader.load("tusk-retro-themes")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


# Match /retro's defaults so the orchestrator output mirrors what the skill
# would have collected. Callers can override via --window-days/--min-recurrence.
DEFAULT_WINDOW_DAYS = 30
DEFAULT_MIN_RECURRENCE = 3


def main(argv: list) -> int:
    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    parser = argparse.ArgumentParser(
        prog="tusk retro",
        description=(
            "Orchestrate a session retrospective: emit retro-signals for the "
            "given task plus the cross-retro themes rollup as a single JSON "
            "blob. Bundles rework_chain and unconsumed_next_steps inside "
            "signals so callers run one subprocess instead of two."
        ),
    )
    parser.add_argument(
        "task_id",
        help="Task ID (integer or TASK-NNN prefix form)",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            "Look-back window for retro-themes. "
            f"Default {DEFAULT_WINDOW_DAYS}. 0 disables the filter."
        ),
    )
    parser.add_argument(
        "--min-recurrence",
        type=int,
        default=DEFAULT_MIN_RECURRENCE,
        help=(
            "Drop themes whose count is below this value. "
            f"Default {DEFAULT_MIN_RECURRENCE}."
        ),
    )
    args = parser.parse_args(argv[2:])

    try:
        task_id = _signals_mod._resolve_task_id(args.task_id)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    if args.window_days < 0:
        print("--window-days must be >= 0", file=sys.stderr)
        return 1
    if args.min_recurrence < 1:
        print("--min-recurrence must be >= 1", file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1
        signals = _signals_mod.build_signals(conn, task_id)
        themes = _themes_mod.fetch_themes(
            conn,
            window_days=args.window_days,
            min_recurrence=args.min_recurrence,
        )
        print(dumps({"task_id": task_id, "signals": signals, "themes": themes}))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk retro <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
