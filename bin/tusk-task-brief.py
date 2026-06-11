#!/usr/bin/env python3
"""Compile durable context for picking up a task.

Usage:
    tusk task-brief <task_id> [--format json|markdown]

JSON output shape:
    {
      "task": {...},
      "acceptance_criteria": [{...}],
      "verification_specs": [{"criterion_id": N, "spec": "..."}],
      "scope": [{"id": N, "pattern": "...", "source": "..."}],
      "entry_points": [{...}],
      "dependencies": {"blocked_by": [...], "dependents": [...]},
      "progress": [{...}],
      "objectives": [{...}],
      "context": {
        "memories": [...],
        "assumptions": [...],
        "open_questions": [...],
        "risks": [...],
        "decisions": [...]
      },
      "context_health_warnings": [
        {"code": "missing_entry_points", "message": "...", "details": {...}}
      ]
    }
"""

import argparse
import os
import re
import shlex
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


PATH_SUFFIX_RE = re.compile(
    r"\.(py|sh|md|json|toml|yaml|yml|txt|swift|js|jsx|ts|tsx|css|html|sql)$"
)
GLOB_CHARS = frozenset("*?[")


def _task_id_type(value: str) -> int:
    raw = value[5:] if value.upper().startswith("TASK-") else value
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid task ID: {value}") from exc


def _rows(rows) -> list[dict]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _clean_path_token(token: str) -> str | None:
    token = token.strip().strip("\"'`.,:;()[]{}")
    if not token or token.startswith("-"):
        return None
    token = token.split("::", 1)[0]
    if token.startswith("./"):
        token = token[2:]
    if token.startswith("/") or ".." in token.split("/"):
        return None
    if "/" not in token and not PATH_SUFFIX_RE.search(token):
        return None
    return token


def _spec_paths(spec: str) -> list[str]:
    try:
        tokens = shlex.split(spec)
    except ValueError:
        tokens = spec.split()
    seen: set[str] = set()
    paths: list[str] = []
    for token in tokens:
        path = _clean_path_token(token)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _scope_is_literal_path(pattern: str) -> bool:
    return bool(pattern) and not any(ch in pattern for ch in GLOB_CHARS)


def _missing_scope_warnings(repo_root: str, scope_rows: list[dict]) -> list[dict]:
    warnings: list[dict] = []
    for row in scope_rows:
        if row["source"] == "unbounded":
            continue
        pattern = row["pattern"]
        if not _scope_is_literal_path(pattern):
            continue
        if not os.path.exists(os.path.join(repo_root, pattern)):
            warnings.append(
                {
                    "code": "missing_scope_path",
                    "message": f"Scope path does not exist: {pattern}",
                    "details": {"scope_id": row["id"], "pattern": pattern},
                }
            )
    return warnings


def _stale_spec_warnings(repo_root: str, criteria_rows: list[dict]) -> list[dict]:
    warnings: list[dict] = []
    for row in criteria_rows:
        spec = row.get("verification_spec")
        if not spec:
            continue
        missing = [
            path for path in _spec_paths(spec) if not os.path.exists(os.path.join(repo_root, path))
        ]
        if missing:
            warnings.append(
                {
                    "code": "stale_verification_spec",
                    "message": f"Verification spec references missing path(s): {', '.join(missing)}",
                    "details": {"criterion_id": row["id"], "missing_paths": missing},
                }
            )
    return warnings


def _context_sections(context_items: list[dict]) -> dict:
    sections = {
        "memories": [],
        "assumptions": [],
        "open_questions": [],
        "risks": [],
        "decisions": [],
    }
    mapping = {
        "memory": "memories",
        "assumption": "assumptions",
        "question": "open_questions",
        "risk": "risks",
        "decision": "decisions",
    }
    for item in context_items:
        key = mapping.get(item["item_type"])
        if key:
            sections[key].append(item)
    return sections


def build_brief(conn: sqlite3.Connection, task_id: int, repo_root: str) -> dict | None:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return None

    criteria = _rows(
        conn.execute(
            "SELECT id, task_id, criterion, source, is_completed, criterion_type, "
            "verification_spec, skip_note, created_at, updated_at "
            "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    )
    progress = _rows(
        conn.execute(
            "SELECT * FROM task_progress WHERE task_id = ? ORDER BY created_at DESC, id DESC",
            (task_id,),
        ).fetchall()
    )
    scope = _rows(
        conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    )
    context_items = _rows(
        conn.execute(
            "SELECT id, task_id, objective_id, item_type, content, status, source, "
            "created_at, updated_at, resolved_at "
            "FROM task_context_items WHERE task_id = ? AND status = 'active' "
            "ORDER BY item_type, id",
            (task_id,),
        ).fetchall()
    )
    entry_points = [item for item in context_items if item["item_type"] == "entry_point"]

    blocked_by = _rows(
        conn.execute(
            "SELECT d.depends_on_id AS task_id, d.relationship_type, t.summary, t.status "
            "FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_id "
            "WHERE d.task_id = ? ORDER BY d.relationship_type, d.depends_on_id",
            (task_id,),
        ).fetchall()
    )
    dependents = _rows(
        conn.execute(
            "SELECT d.task_id, d.relationship_type, t.summary, t.status "
            "FROM task_dependencies d JOIN tasks t ON t.id = d.task_id "
            "WHERE d.depends_on_id = ? ORDER BY d.relationship_type, d.task_id",
            (task_id,),
        ).fetchall()
    )
    objectives = _rows(
        conn.execute(
            "SELECT o.id, o.summary, o.description, o.status, ot.relationship_type, "
            "ot.created_at AS linked_at "
            "FROM objective_tasks ot JOIN objectives o ON o.id = ot.objective_id "
            "WHERE ot.task_id = ? ORDER BY o.status, o.id",
            (task_id,),
        ).fetchall()
    )

    verification_specs = [
        {
            "criterion_id": row["id"],
            "criterion": row["criterion"],
            "type": row["criterion_type"],
            "spec": row["verification_spec"],
        }
        for row in criteria
        if row.get("verification_spec")
    ]

    warnings = []
    if not entry_points:
        warnings.append(
            {
                "code": "missing_entry_points",
                "message": "No active entry_point context items are attached to this task.",
                "details": {"task_id": task_id},
            }
        )
    warnings.extend(_missing_scope_warnings(repo_root, scope))
    warnings.extend(_stale_spec_warnings(repo_root, criteria))

    return {
        "task": {key: task[key] for key in task.keys()},
        "acceptance_criteria": criteria,
        "verification_specs": verification_specs,
        "scope": scope,
        "entry_points": entry_points,
        "dependencies": {"blocked_by": blocked_by, "dependents": dependents},
        "progress": progress,
        "objectives": objectives,
        "context": _context_sections(context_items),
        "context_health_warnings": warnings,
    }


def _markdown_list(items: list[str]) -> str:
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)


def render_markdown(brief: dict) -> str:
    task = brief["task"]
    criteria = [
        f"[{'x' if row['is_completed'] else ' '}] {row['criterion']}"
        for row in brief["acceptance_criteria"]
    ]
    specs = [
        f"Criterion {row['criterion_id']}: `{row['spec']}`"
        for row in brief["verification_specs"]
    ]
    scope = [
        f"{row['pattern']} ({row['source']})"
        for row in brief["scope"]
    ]
    entry_points = [row["content"] for row in brief["entry_points"]]
    warnings = [
        f"{row['code']}: {row['message']}"
        for row in brief["context_health_warnings"]
    ]
    progress = []
    for row in brief["progress"][:5]:
        text = row.get("next_steps") or row.get("note") or row.get("commit_message")
        if text:
            progress.append(text)

    return "\n".join(
        [
            f"# TASK-{task['id']}: {task['summary']}",
            "",
            f"Status: {task['status']} | Priority: {task['priority']} | Complexity: {task['complexity']}",
            "",
            "## Description",
            task.get("description") or "None",
            "",
            "## Criteria",
            _markdown_list(criteria),
            "",
            "## Verification",
            _markdown_list(specs),
            "",
            "## Scope",
            _markdown_list(scope),
            "",
            "## Entry Points",
            _markdown_list(entry_points),
            "",
            "## Recent Progress",
            _markdown_list(progress),
            "",
            "## Context Health",
            _markdown_list(warnings),
        ]
    )


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused)
    repo_root = argv[2]

    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk task-brief",
        description="Compile durable task context for a fresh session.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("task_id", type=_task_id_type, help="Task ID (integer or TASK-NNN form)")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        dest="fmt",
        help="Output format. json returns the compiled context packet; markdown renders a concise pickup brief.",
    )
    args = parser.parse_args(argv[3:])

    conn = get_connection(db_path)
    try:
        brief = build_brief(conn, args.task_id, repo_root)
        if brief is None:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 1
        if args.fmt == "markdown":
            print(render_markdown(brief))
        else:
            print(dumps(brief))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 3 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-brief <task_id> [--format json|markdown]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
