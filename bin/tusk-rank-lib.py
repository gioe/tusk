"""Shared WSJF ranking helpers for tusk-task-select and tusk-task-start.

Both scripts pick "the top ready task" from the v_ready_tasks view and they
must agree on the ordering, column projection, and optional filters. Keeping
the SQL in one place means a future change to the WSJF ranking logic (or the
column list returned to the caller) can't silently drift between the two
entry points.

Imported via tusk_loader (hyphenated filename requires it):

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _rank_lib = tusk_loader.load("tusk-rank-lib")
    select_top_ready_task = _rank_lib.select_top_ready_task
    empty_backlog_message = _rank_lib.empty_backlog_message
    COMPLEXITY_ORDER = _rank_lib.COMPLEXITY_ORDER
"""

import sqlite3
from typing import Iterable, Optional


COMPLEXITY_ORDER = ["XS", "S", "M", "L", "XL"]


def select_top_ready_task(
    conn: sqlite3.Connection,
    *,
    max_complexity: Optional[str] = None,
    exclude_ids: Optional[Iterable[int]] = None,
) -> Optional[sqlite3.Row]:
    """Return the top WSJF-ranked ready task row, or None when nothing matches.

    The projected columns (id, summary, priority, priority_score, domain,
    assignee, complexity, description) match what tusk-task-select has
    historically returned; tusk-task-start only needs `id` from the result and
    re-fetches the full tasks row itself.

    max_complexity: if set, restrict to tasks whose complexity is at or below
        the given tier (XS < S < M < L < XL).
    exclude_ids: if set, skip any task IDs in the iterable.
    """
    conditions: list[str] = []
    params: list = []

    if max_complexity:
        idx = COMPLEXITY_ORDER.index(max_complexity)
        allowed = COMPLEXITY_ORDER[: idx + 1]
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"complexity IN ({placeholders})")
        params.extend(allowed)

    exclude_list = list(exclude_ids) if exclude_ids else []
    if exclude_list:
        placeholders = ",".join("?" * len(exclude_list))
        conditions.append(f"id NOT IN ({placeholders})")
        params.extend(exclude_list)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
SELECT id, summary, priority, priority_score, domain, assignee, complexity, description
FROM v_ready_tasks
{where_clause}
ORDER BY priority_score DESC, id
LIMIT 1
"""
    return conn.execute(sql, params).fetchone()


def empty_backlog_message(
    *,
    max_complexity: Optional[str] = None,
    exclude_ids: Optional[Iterable[int]] = None,
) -> str:
    """Return the stderr message tusk-task-select has historically emitted
    when the ranking query returns no rows.

    Kept byte-for-byte compatible with the pre-refactor format so callers
    (including the shell-level skills and /loop) don't have to care which
    script emitted the message.
    """
    msg = "No ready tasks found"
    if max_complexity:
        msg += f" with complexity at or below {max_complexity}"
    exclude_list = list(exclude_ids) if exclude_ids else []
    if exclude_list:
        n = len(exclude_list)
        msg += f" (excluding {n} task ID{'s' if n != 1 else ''})"
    return msg
