#!/usr/bin/env python3
"""Recalculate WSJF priority scores for all open tasks.

Called by the tusk wrapper:
    tusk wsjf

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for dispatch consistency, unused)

Scoring formula:
    score = ROUND(
        (base_priority + source_bonus + unblocks_bonus + contingent_penalty)
        / complexity_weight
    )

Where:
    base_priority     — numeric value from priority label (Highest=100 … Lowest=20)
    source_bonus      — +10 if not deferred
    unblocks_bonus    — MIN(count_of_tasks_this_unblocks * 5, 15)
    contingent_penalty— -10 if task has only contingent (not blocks) dependencies
    complexity_weight — XS=1, S=2, M=3, L=5, XL=8
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


def recalculate_wsjf(db_path: str) -> int:
    conn = get_connection(db_path)

    cursor = conn.execute("""
        UPDATE tasks SET priority_score = ROUND(
            (
                CASE priority
                    WHEN 'Highest' THEN 100
                    WHEN 'High'    THEN 80
                    WHEN 'Medium'  THEN 60
                    WHEN 'Low'     THEN 40
                    WHEN 'Lowest'  THEN 20
                    ELSE 40
                END
                + CASE WHEN is_deferred = 0 THEN 10 ELSE 0 END
                + MIN(COALESCE((
                    SELECT COUNT(*) * 5
                    FROM task_dependencies d
                    WHERE d.depends_on_id = tasks.id
                ), 0), 15)
                + CASE WHEN EXISTS (
                    SELECT 1 FROM task_dependencies d
                    WHERE d.task_id = tasks.id AND d.relationship_type = 'contingent'
                ) AND NOT EXISTS (
                    SELECT 1 FROM task_dependencies d
                    WHERE d.task_id = tasks.id AND d.relationship_type = 'blocks'
                ) THEN -10 ELSE 0 END
            ) * 1.0
            / CASE complexity
                WHEN 'XS' THEN 1
                WHEN 'S'  THEN 2
                WHEN 'M'  THEN 3
                WHEN 'L'  THEN 5
                WHEN 'XL' THEN 8
                ELSE 3
            END
        )
        WHERE status <> 'Done'
    """)

    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: tusk wsjf", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    count = recalculate_wsjf(db_path)
    print(f"WSJF scoring complete: {count} tasks updated")


if __name__ == "__main__":
    main()
