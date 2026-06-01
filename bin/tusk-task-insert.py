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
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-git-helpers.py, tusk-json-lib.py

TUSK_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")

_db_lib = tusk_loader.load("tusk-db-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config
validate_enum = _db_lib.validate_enum
extract_paths = _git_helpers.extract_paths
is_prose_identifier_path = _git_helpers.is_prose_identifier_path
path_exists_in_repo = _git_helpers.path_exists_in_repo


_RELATIVE_NOT_BEFORE_RE = re.compile(r"^\+(\d+)([mhdw])$")
_GLOB_METACHARS = set("*?[")
_OBVIOUS_REPO_PATH_RE = re.compile(
    r'(?:^|[\s\'"`(,])'
    r'((?:apps|app|src|test|tests|bin|docs|doc|skills|skills-internal|hooks)/'
    r'[\w./_-]+)',
    re.MULTILINE,
)


def _format_utc(value: datetime) -> str:
    """Return a SQLite-friendly UTC timestamp."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_not_before(value: str) -> str:
    """Parse --not-before as UTC, accepting ISO datetimes or +Nm/+Nh/+Nd/+Nw."""
    raw = (value or "").strip()
    match = _RELATIVE_NOT_BEFORE_RE.match(raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return _format_utc(datetime.now(timezone.utc) + delta)

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--not-before must be an ISO datetime (for example "
            "2026-06-01T06:00:00Z) or a relative offset like +4h"
        ) from exc
    return _format_utc(parsed)


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
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
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


def _repo_root(config_path: str) -> str | None:
    env_root = os.environ.get("TUSK_REPO_ROOT") or os.environ.get("TUSK_PROJECT")
    if env_root:
        return env_root
    if not config_path:
        return None
    config_dir = os.path.dirname(os.path.abspath(config_path))
    if os.path.basename(config_dir) == "tusk":
        return os.path.dirname(config_dir)
    return config_dir


def _has_glob_metachar(path: str) -> bool:
    return any(ch in path for ch in _GLOB_METACHARS)


def _expand_scope_patterns(patterns: list[str]) -> list[str]:
    expanded = []
    for pattern in patterns:
        for entry in str(pattern or "").split(","):
            entry = entry.strip()
            if entry:
                expanded.append(entry)
    return expanded


def _obvious_spec_paths(spec: str) -> list[str]:
    paths = []
    seen = set()
    for path in extract_paths(spec):
        if path not in seen:
            seen.add(path)
            paths.append(path)
    for match in _OBVIOUS_REPO_PATH_RE.finditer(spec or ""):
        path = match.group(1).strip().rstrip('.,;:\'"`)')
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _warn_missing_path(path: str, source: str) -> None:
    print(
        f"Warning: task-insert {source} path does not exist at repo root: {path}",
        file=sys.stderr,
    )


def _warn_for_missing_declared_paths(
    repo_root: str | None,
    scope_patterns: list[str],
    typed_criteria: list[dict],
) -> None:
    if not repo_root:
        return

    for pattern in scope_patterns:
        if _has_glob_metachar(pattern):
            continue
        if not path_exists_in_repo(repo_root, pattern):
            _warn_missing_path(pattern, "--scope")

    warned_specs: set[str] = set()
    for tc in typed_criteria:
        spec = tc.get("spec") or ""
        for path in _obvious_spec_paths(spec):
            if _has_glob_metachar(path) or path in warned_specs:
                continue
            warned_specs.add(path)
            if not path_exists_in_repo(repo_root, path):
                _warn_missing_path(path, "verification_spec")


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
    parser.add_argument("--workflow", default=None, help="Workflow (validated against config)")
    parser.add_argument("--expires-in", type=int, default=None, dest="expires_in_days", metavar="DAYS",
                        help="Set expires_at to +N days")
    parser.add_argument("--not-before", type=_parse_not_before, default=None, dest="not_before",
                        metavar="TIMESTAMP",
                        help="Do not surface/start the task before this UTC time; accepts ISO or +Nm/+Nh/+Nd/+Nw")
    parser.add_argument("--fixes-task-id", type=int, default=None, dest="fixes_task_id", metavar="ID",
                        help="Link this task as a follow-up/rework of the given task id")
    parser.add_argument("--scope", action="append", default=[], metavar="PATTERN",
                        help="Declare an in-scope path (source='operator_declared'). Repeatable.")
    parser.add_argument("--creates", action="append", default=[], metavar="PATH",
                        help="Declare a path the task will create (source='creates'). Repeatable.")
    parser.add_argument("--unbounded", action="store_true", default=False,
                        help="Mark this task as legitimately spanning the repo — emits an 'unbounded' "
                             "scope row that signals the commit-time scope guard to silently pass.")
    args = parser.parse_args(argv[2:])

    summary = args.summary
    description = args.description
    priority = args.priority
    domain = args.domain
    task_type = args.task_type
    assignee = args.assignee
    complexity = args.complexity
    workflow = args.workflow
    criteria: list[str] = args.criteria
    typed_criteria: list[dict] = args.typed_criteria
    expires_in_days = args.expires_in_days
    not_before = args.not_before
    fixes_task_id = args.fixes_task_id
    scope_patterns: list[str] = _expand_scope_patterns(args.scope)
    creates_paths: list[str] = args.creates
    unbounded: bool = args.unbounded

    if not criteria and not typed_criteria:
        parser.error(
            "at least one acceptance criterion is required. "
            "Use --criteria \"...\" or --typed-criteria '{\"text\":\"...\"}' to add one."
        )

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

    if workflow is not None:
        err = validate_enum(workflow, config.get("workflows", []), "workflow")
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

    repo_root = _repo_root(config_path)
    _warn_for_missing_declared_paths(repo_root, scope_patterns, typed_criteria)

    # Run duplicate check
    dupe = run_dupe_check(summary, domain)
    if dupe:
        result = {
            "duplicate": True,
            "matched_task_id": dupe.get("id"),
            "matched_summary": dupe.get("summary", ""),
            "similarity": dupe.get("similarity", 0),
        }
        print(dumps(result))
        return 1

    # Compute expires_at
    expires_at_expr = None
    if expires_in_days is not None:
        expires_at_expr = f"+{expires_in_days} days"

    # Insert task + criteria in one transaction
    conn = get_connection(db_path)
    try:
        if fixes_task_id is not None:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (fixes_task_id,)
            ).fetchone()
            if row is None:
                print(
                    f"Error: --fixes-task-id {fixes_task_id} does not reference an existing task",
                    file=sys.stderr,
                )
                return 2

        if expires_at_expr:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, workflow, fixes_task_id, "
                "expires_at, not_before, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, ?, datetime('now', ?), "
                "?, datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, workflow, fixes_task_id, expires_at_expr, not_before),
            )
        else:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, workflow, fixes_task_id, "
                "not_before, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, workflow, fixes_task_id, not_before),
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

        for pattern in scope_patterns:
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (?, ?, 'operator_declared')",
                (task_id, pattern),
            )
        for path in creates_paths:
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (?, ?, 'creates')",
                (task_id, path),
            )
        if unbounded:
            # Pattern is a sentinel — the scope guard short-circuits when any
            # row for the task has source='unbounded' (emits no patterns,
            # silent pass).
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (?, '**', 'unbounded')",
                (task_id,),
            )
        else:
            # Auto-extract paths from summary/description/criteria/specs.
            # Mirrors the migration-73 backfill (which used
            # task_referenced_paths) so new tasks land with the same
            # task_scope shape that the scope-paths fallback would have
            # inferred from a legacy task. Explicit --scope and --creates
            # rows already inserted above win — auto_derived is only added
            # for paths the operator didn't already declare.
            explicit_patterns = set(scope_patterns) | set(creates_paths)
            text_blocks = [summary or "", description or ""]
            for c in criteria:
                text_blocks.append(c or "")
            for tc in typed_criteria:
                text_blocks.append(tc.get("text") or "")
                text_blocks.append(tc.get("spec") or "")
            seen_auto: set = set()
            for text in text_blocks:
                for p in extract_paths(text):
                    if is_prose_identifier_path(p, repo_root):
                        continue
                    if p in explicit_patterns or p in seen_auto:
                        continue
                    seen_auto.add(p)
                    conn.execute(
                        "INSERT INTO task_scope (task_id, pattern, source) "
                        "VALUES (?, ?, 'auto_derived')",
                        (task_id, p),
                    )

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
    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-insert \"<summary>\" \"<description>\" [--priority P] [--domain D]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
