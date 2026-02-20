#!/usr/bin/env python3
"""Insert a new task with optional criteria in one atomic operation.

Called by the tusk wrapper:
    tusk task-insert "<summary>" "<description>" [flags...]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — summary, description, and optional flags

Flags:
    --priority <p>        Priority (default: Medium)
    --domain <d>          Domain (default: NULL)
    --task-type <t>       Task type (default: feature)
    --assignee <a>        Assignee (default: NULL)
    --complexity <c>      Complexity (default: M)
    --criteria <text>     Acceptance criterion (repeatable, type=manual)
    --typed-criteria <json>  Typed criterion as JSON (repeatable)
                             Format: {"text":"...","type":"...","spec":"..."}
    --deferred            Set expires_at to +60 days and prefix summary with [Deferred]
    --expires-in <days>   Set expires_at to +N days

Internally validates all enum values against config, runs duplicate
detection, and inserts the task + criteria in one transaction.

Exit codes:
    0 — success (prints JSON with task_id)
    1 — duplicate found (prints JSON with duplicate info)
    2 — validation or database error
"""

import json
import sqlite3
import subprocess
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def validate_enum(value, valid_values: list, field_name: str) -> str | None:
    """Validate a value against a config list. Returns error message or None."""
    if not valid_values:
        return None  # empty list = no validation
    if value not in valid_values:
        joined = ", ".join(valid_values)
        return f"Invalid {field_name} '{value}'. Valid: {joined}"
    return None


def run_dupe_check(summary: str, domain: str | None) -> dict | None:
    """Run tusk dupes check and return match info if duplicate found."""
    cmd = ["tusk", "dupes", "check", summary, "--json"]
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
    if len(argv) < 4:
        print(
            "Usage: tusk task-insert \"<summary>\" \"<description>\" [--priority P] "
            "[--domain D] [--task-type T] [--assignee A] [--complexity C] "
            "[--criteria \"...\"] [--deferred] [--expires-in DAYS]",
            file=sys.stderr,
        )
        return 2

    db_path = argv[0]
    config_path = argv[1]
    remaining = argv[2:]

    # Parse positional args and flags
    summary = None
    description = None
    priority = "Medium"
    domain = None
    task_type = "feature"
    assignee = None
    complexity = "M"
    criteria: list[str] = []
    typed_criteria: list[dict] = []
    deferred = False
    expires_in_days = None

    i = 0
    while i < len(remaining):
        arg = remaining[i]
        if arg == "--priority":
            if i + 1 >= len(remaining):
                print("Error: --priority requires a value", file=sys.stderr)
                return 2
            priority = remaining[i + 1]
            i += 2
        elif arg == "--domain":
            if i + 1 >= len(remaining):
                print("Error: --domain requires a value", file=sys.stderr)
                return 2
            domain = remaining[i + 1]
            i += 2
        elif arg == "--task-type":
            if i + 1 >= len(remaining):
                print("Error: --task-type requires a value", file=sys.stderr)
                return 2
            task_type = remaining[i + 1]
            i += 2
        elif arg == "--assignee":
            if i + 1 >= len(remaining):
                print("Error: --assignee requires a value", file=sys.stderr)
                return 2
            assignee = remaining[i + 1]
            i += 2
        elif arg == "--complexity":
            if i + 1 >= len(remaining):
                print("Error: --complexity requires a value", file=sys.stderr)
                return 2
            complexity = remaining[i + 1]
            i += 2
        elif arg == "--criteria":
            if i + 1 >= len(remaining):
                print("Error: --criteria requires a value", file=sys.stderr)
                return 2
            criteria.append(remaining[i + 1])
            i += 2
        elif arg == "--typed-criteria":
            if i + 1 >= len(remaining):
                print("Error: --typed-criteria requires a value", file=sys.stderr)
                return 2
            try:
                tc = json.loads(remaining[i + 1])
            except json.JSONDecodeError as e:
                print(f"Error: --typed-criteria must be valid JSON: {e}", file=sys.stderr)
                return 2
            if not isinstance(tc, dict) or "text" not in tc:
                print('Error: --typed-criteria must have at least a "text" key', file=sys.stderr)
                return 2
            typed_criteria.append(tc)
            i += 2
        elif arg == "--deferred":
            deferred = True
            i += 1
        elif arg == "--expires-in":
            if i + 1 >= len(remaining):
                print("Error: --expires-in requires a value (days)", file=sys.stderr)
                return 2
            try:
                expires_in_days = int(remaining[i + 1])
            except ValueError:
                print(f"Error: --expires-in must be an integer, got: {remaining[i + 1]}", file=sys.stderr)
                return 2
            i += 2
        elif summary is None:
            summary = arg
            i += 1
        elif description is None:
            description = arg
            i += 1
        else:
            print(f"Error: Unexpected argument: {arg}", file=sys.stderr)
            return 2

    if not summary:
        print("Error: summary is required", file=sys.stderr)
        return 2
    if not description:
        print("Error: description is required", file=sys.stderr)
        return 2

    # Apply --deferred: prefix summary and set default expiry
    if deferred:
        if not summary.startswith("[Deferred]"):
            summary = f"[Deferred] {summary}"
        if expires_in_days is None:
            expires_in_days = 60

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
                "task_type, assignee, complexity, expires_at, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, datetime('now', ?), "
                "datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, expires_at_expr),
            )
        else:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity),
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
    subprocess.run(["tusk", "wsjf"], capture_output=True)

    result = {
        "task_id": task_id,
        "summary": summary,
        "criteria_ids": criteria_ids,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
