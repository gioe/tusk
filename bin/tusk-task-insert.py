#!/usr/bin/env python3
"""Insert a new task with optional criteria in one atomic operation.

Called by the tusk wrapper:
    tusk task-insert "<summary>" "<description>" [flags...]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — summary, description, and optional flags

Run 'tusk task-insert --help' for the full flag reference.

Internally validates all enum values against config, runs duplicate
detection, and inserts the task + criteria in one transaction.

Exit codes:
    0 — success (prints JSON with task_id)
    1 — duplicate found (prints JSON with duplicate info)
    2 — validation or database error
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

TUSK_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")


def _load_db_lib():
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-db-lib.py")
    _s = importlib.util.spec_from_file_location("tusk_db_lib", _p)
    _m = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(_m)
    return _m


_db_lib = _load_db_lib()
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


def validate_enum(value, valid_values: list, field_name: str) -> str | None:
    """Validate a value against a config list. Returns error message or None."""
    if not valid_values:
        return None  # empty list = no validation
    if value not in valid_values:
        joined = ", ".join(valid_values)
        return f"Invalid {field_name} '{value}'. Valid: {joined}"
    return None


def _typed_criterion_type(value: str) -> dict:
    """Parse a JSON string into a typed-criteria dict."""
    try:
        tc = json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"--typed-criteria must be valid JSON: {e}")
    if not isinstance(tc, dict) or "text" not in tc:
        raise argparse.ArgumentTypeError('--typed-criteria must have at least a "text" key')
    return tc


def run_dupe_check(summary: str, domain: str | None) -> dict | None:
    """Run tusk dupes check and return match info if duplicate found."""
    cmd = [TUSK_BIN, "dupes", "check", summary, "--json"]
    if domain:
        cmd.extend(["--domain", domain])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 1:
        # Duplicate found
        try:
            data = json.loads(result.stdout)
            dupes = data.get("duplicates", [])
            if dupes:
                return dupes[0]  # highest similarity match
        except json.JSONDecodeError:
            pass
        return {"id": "unknown", "similarity": 0}
    return None


def main(argv: list[str]) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(
        prog="tusk task-insert",
        description="Insert a new task with criteria in one atomic operation",
    )
    parser.add_argument("summary", help="Task summary")
    parser.add_argument("description", help="Task description")
    parser.add_argument("--priority", default="Medium", help="Priority (default: Medium)")
    parser.add_argument("--domain", default=None, help="Domain")
    parser.add_argument("--task-type", default="feature", dest="task_type", help="Task type (default: feature)")
    parser.add_argument("--assignee", default=None, help="Assignee")
    parser.add_argument("--complexity", default="M", help="Complexity (default: M)")
    parser.add_argument("--criteria", action="append", default=[], metavar="TEXT",
                        help="Acceptance criterion text (repeatable)")
    parser.add_argument("--typed-criteria", action="append", default=[], type=_typed_criterion_type,
                        dest="typed_criteria", metavar="JSON",
                        help='Typed criterion as JSON, e.g. \'{"text":"...","type":"...","spec":"..."}\' (repeatable)')
    parser.add_argument("--deferred", action="store_true", help="Mark task as deferred (+60d expiry)")
    parser.add_argument("--expires-in", type=int, default=None, dest="expires_in_days", metavar="DAYS",
                        help="Set expires_at to +N days")
    args = parser.parse_args(argv[2:])

    summary = args.summary
    description = args.description
    priority = args.priority
    domain = args.domain
    task_type = args.task_type
    assignee = args.assignee
    complexity = args.complexity
    criteria: list[str] = args.criteria
    typed_criteria: list[dict] = args.typed_criteria
    deferred = args.deferred
    expires_in_days = args.expires_in_days

    if not criteria and not typed_criteria:
        parser.error(
            "at least one acceptance criterion is required. "
            "Use --criteria \"...\" or --typed-criteria '{\"text\":\"...\"}' to add one."
        )

    # Apply --deferred: prefix summary and set default expiry
    if deferred:
        if not summary.startswith("[Deferred]"):
            summary = f"[Deferred] {summary}"
        if expires_in_days is None:
            expires_in_days = 60

    # is_deferred=1 whenever summary starts with [Deferred] (covers --deferred flag and manual prefix)
    is_deferred = 1 if summary.startswith("[Deferred]") else 0

    # Load and validate against config
    config = load_config(config_path)

    errors = []
    err = validate_enum(priority, config.get("priorities", []), "priority")
    if err:
        errors.append(err)
    err = validate_enum(task_type, config.get("task_types", []), "task_type")
    if err:
        errors.append(err)
    err = validate_enum(complexity, config.get("complexity", []), "complexity")
    if err:
        errors.append(err)

    if domain is not None:
        err = validate_enum(domain, config.get("domains", []), "domain")
        if err:
            errors.append(err)

    agents = config.get("agents", {})
    if assignee is not None and agents:
        valid_agents = list(agents.keys())
        err = validate_enum(assignee, valid_agents, "assignee")
        if err:
            errors.append(err)

    # Validate typed criteria
    criterion_types = config.get("criterion_types", [])
    spec_required_types = {"code", "test", "file"}
    for i, tc in enumerate(typed_criteria):
        ct = tc.get("type", "manual")
        if criterion_types and ct not in criterion_types:
            joined = ", ".join(criterion_types)
            errors.append(f"--typed-criteria[{i}]: invalid type '{ct}'. Valid: {joined}")
        if ct in spec_required_types and not tc.get("spec"):
            errors.append(f"--typed-criteria[{i}]: --spec required for type '{ct}'")

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        return 2

    # Run duplicate check
    dupe = run_dupe_check(summary, domain)
    if dupe:
        result = {
            "duplicate": True,
            "matched_task_id": dupe.get("id"),
            "matched_summary": dupe.get("summary", ""),
            "similarity": dupe.get("similarity", 0),
        }
        print(json.dumps(result, indent=2))
        return 1

    # Compute expires_at
    expires_at_expr = None
    if expires_in_days is not None:
        expires_at_expr = f"+{expires_in_days} days"

    # Insert task + criteria in one transaction
    conn = get_connection(db_path)
    try:
        if expires_at_expr:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, is_deferred, expires_at, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, datetime('now', ?), "
                "datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, is_deferred, expires_at_expr),
            )
        else:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, is_deferred, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, is_deferred),
            )

        task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        criteria_ids = []
        for criterion in criteria:
            conn.execute(
                "INSERT INTO acceptance_criteria (task_id, criterion, source) "
                "VALUES (?, ?, 'original')",
                (task_id, criterion),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            criteria_ids.append(cid)

        for tc in typed_criteria:
            conn.execute(
                "INSERT INTO acceptance_criteria "
                "(task_id, criterion, source, criterion_type, verification_spec) "
                "VALUES (?, ?, 'original', ?, ?)",
                (task_id, tc["text"], tc.get("type", "manual"), tc.get("spec")),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            criteria_ids.append(cid)

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"Database error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    # Run WSJF scoring so the new task gets a priority_score immediately
    subprocess.run([TUSK_BIN, "wsjf"], capture_output=True)

    result = {
        "task_id": task_id,
        "summary": summary,
        "criteria_ids": criteria_ids,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-insert \"<summary>\" \"<description>\" [--priority P] [--domain D]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
