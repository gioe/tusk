#!/usr/bin/env python3
"""DB health audit across 6 categories.

Called by the tusk wrapper:
    tusk audit [--json]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — optional flags (--json)

Output JSON shape:
    {
        "config_fitness":    N,
        "task_hygiene":      N,
        "dependency_health": N,
        "session_gaps":      N,
        "criteria_gaps":     N,
        "scoring_gaps":      N
    }

All six categories are always present, even when the count is zero.
Output is always JSON (--json flag is accepted but has no effect).
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def _build_config_fitness_expr(config: dict) -> str:
    """Build the config_fitness SQL sub-expression from loaded config.

    Returns a SQL expression (no trailing semicolon) that evaluates to the
    number of open tasks whose domain, assignee, or task_type contains a value
    that is no longer present in the current config.

    If all relevant config arrays are empty (no validation configured),
    returns '0'.
    """
    conditions: list[str] = []

    domains = config.get("domains", [])
    if domains:
        quoted = ", ".join(f"'{d}'" for d in domains)
        conditions.append(
            f"(domain IS NOT NULL AND domain <> '' AND domain NOT IN ({quoted}))"
        )

    agents = config.get("agents", {})
    agent_keys = list(agents.keys()) if isinstance(agents, dict) else []
    if agent_keys:
        quoted = ", ".join(f"'{a}'" for a in agent_keys)
        conditions.append(
            f"(assignee IS NOT NULL AND assignee <> '' AND assignee NOT IN ({quoted}))"
        )

    task_types = config.get("task_types", [])
    if task_types:
        quoted = ", ".join(f"'{t}'" for t in task_types)
        conditions.append(
            f"(task_type IS NOT NULL AND task_type NOT IN ({quoted}))"
        )

    if not conditions:
        return "0"

    where_clause = " OR ".join(conditions)
    return f"(SELECT COUNT(*) FROM tasks WHERE status <> 'Done' AND ({where_clause}))"


def run_audit(db_path: str, config: dict) -> dict:
    conn = get_connection(db_path)
    try:
        config_fitness_expr = _build_config_fitness_expr(config)

        sql = f"""
SELECT
  {config_fitness_expr} as config_fitness,

  (SELECT COUNT(*) FROM tasks WHERE
    (status = 'Done' AND (closed_reason IS NULL OR closed_reason = ''))
    OR (status = 'In Progress' AND id NOT IN (SELECT DISTINCT task_id FROM task_sessions))
    OR (expires_at IS NOT NULL AND expires_at < datetime('now') AND status <> 'Done')
    OR (description IS NULL OR description = '')
    OR (complexity IS NULL OR complexity = '')
  ) as task_hygiene,

  (SELECT COUNT(DISTINCT d.task_id) FROM task_dependencies d
    JOIN tasks dep ON d.task_id = dep.id
    JOIN tasks blocker ON d.depends_on_id = blocker.id
    WHERE dep.status <> 'Done'
      AND blocker.status = 'Done'
      AND blocker.closed_reason IN ('wont_do', 'duplicate')
  ) as dependency_health,

  (SELECT COUNT(*) FROM task_sessions WHERE ended_at IS NULL)
  + (SELECT COUNT(*) FROM tasks WHERE status = 'Done'
      AND id NOT IN (SELECT DISTINCT task_id FROM task_sessions))
  as session_gaps,

  (SELECT COUNT(*) FROM tasks WHERE status IN ('In Progress', 'Done')
    AND id NOT IN (SELECT DISTINCT task_id FROM acceptance_criteria))
  + (SELECT COUNT(*) FROM tasks WHERE status = 'Done'
    AND id IN (SELECT task_id FROM acceptance_criteria WHERE is_completed = 0))
  as criteria_gaps,

  (SELECT COUNT(*) FROM tasks
    WHERE status = 'To Do'
    AND (priority_score IS NULL OR priority_score = 0))
  as scoring_gaps
"""
        row = conn.execute(sql).fetchone()
        return {
            "config_fitness":    row["config_fitness"],
            "task_hygiene":      row["task_hygiene"],
            "dependency_health": row["dependency_health"],
            "session_gaps":      row["session_gaps"],
            "criteria_gaps":     row["criteria_gaps"],
            "scoring_gaps":      row["scoring_gaps"],
        }
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: tusk audit [--json]", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]

    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"audit: could not load config: {exc}", file=sys.stderr)
        sys.exit(1)

    result = run_audit(db_path, config)
    print(dumps(result))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk audit [--json]", file=sys.stderr)
        sys.exit(1)
    main()
