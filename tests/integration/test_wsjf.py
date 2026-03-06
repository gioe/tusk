"""Integration tests for WSJF scoring (bin/tusk wsjf).

Uses the db_path fixture (a real initialised SQLite DB), inserts tasks with
known priority/complexity combinations, calls `bin/tusk wsjf` via subprocess,
then queries priority_score and asserts exact expected values.

Formula (from cmd_wsjf in bin/tusk):
  priority_score = ROUND(
    (base_priority + deferred_bonus + unblocks_bonus + contingent_penalty)
    / complexity_weight
  )

  base_priority    : Highest=100, High=80, Medium=60, Low=40, Lowest=20
  deferred_bonus   : is_deferred=0 → +10, is_deferred=1 → +0
  unblocks_bonus   : MIN(COUNT(dependents) * 5, 15)  [all relationship types]
  contingent_penalty: -10 if task has ≥1 contingent dep AND no blocks dep
  complexity_weight: XS=1, S=2, M=3, L=5, XL=8
"""

import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_wsjf(db_path) -> None:
    """Call `bin/tusk wsjf` against the given database."""
    env = {**os.environ, "TUSK_DB": str(db_path)}
    result = subprocess.run(
        [TUSK_BIN, "wsjf"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"tusk wsjf failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def insert_task(
    conn: sqlite3.Connection,
    summary: str,
    *,
    status: str = "To Do",
    priority: str = "Medium",
    complexity: str = "M",
    is_deferred: int = 0,
) -> int:
    """Insert a task and return its id."""
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type, is_deferred, priority_score)
        VALUES (?, ?, ?, ?, 'feature', ?, 0)
        """,
        (summary, status, priority, complexity, is_deferred),
    )
    conn.commit()
    return cur.lastrowid


def add_dep(conn: sqlite3.Connection, task_id: int, depends_on_id: int, rel: str = "blocks") -> None:
    """Insert a task_dependency row."""
    conn.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type)
        VALUES (?, ?, ?)
        """,
        (task_id, depends_on_id, rel),
    )
    conn.commit()


def get_score(conn: sqlite3.Connection, task_id: int) -> int:
    """Return the priority_score for a task after WSJF has run."""
    row = conn.execute(
        "SELECT priority_score FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row is not None, f"Task {task_id} not found"
    return row[0]


# ---------------------------------------------------------------------------
# Parameterised: priority × complexity (8 cases, no deps, non-deferred)
# ---------------------------------------------------------------------------

# (priority, complexity, expected_score)
# score = ROUND((base + 10) / weight)
PRIORITY_COMPLEXITY_CASES = [
    ("Highest", "XS", 110),   # ROUND((100+10)/1) = 110
    ("High",    "S",   45),   # ROUND((80+10)/2)  = 45
    ("Medium",  "M",   23),   # ROUND((60+10)/3)  = 23  (70/3=23.33→23)
    ("Low",     "L",   10),   # ROUND((40+10)/5)  = 10
    ("Lowest",  "XL",   4),   # ROUND((20+10)/8)  = 4   (30/8=3.75→4)
    ("Medium",  "XS",  70),   # ROUND((60+10)/1)  = 70
    ("High",    "XL",  11),   # ROUND((80+10)/8)  = 11  (90/8=11.25→11)
    ("Low",     "S",   25),   # ROUND((40+10)/2)  = 25
]


@pytest.mark.parametrize("priority,complexity,expected", PRIORITY_COMPLEXITY_CASES)
def test_priority_complexity_score(db_path, priority, complexity, expected):
    """Each priority × complexity combination produces the exact expected score."""
    conn = sqlite3.connect(str(db_path))
    try:
        tid = insert_task(conn, f"{priority}/{complexity} task", priority=priority, complexity=complexity)
    finally:
        conn.close()

    run_wsjf(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert get_score(conn, tid) == expected
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deferred bonus
# ---------------------------------------------------------------------------

class TestDeferredBonus:
    def test_non_deferred_receives_bonus(self, db_path):
        """is_deferred=0 adds +10 to the numerator."""
        conn = sqlite3.connect(str(db_path))
        try:
            tid = insert_task(conn, "non-deferred task", priority="Medium", complexity="M", is_deferred=0)
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # (60 + 10) / 3 = 23
            assert get_score(conn, tid) == 23
        finally:
            conn.close()

    def test_deferred_receives_no_bonus(self, db_path):
        """is_deferred=1 does not add the +10 bonus."""
        conn = sqlite3.connect(str(db_path))
        try:
            tid = insert_task(conn, "deferred task", priority="Medium", complexity="M", is_deferred=1)
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # (60 + 0) / 3 = 20
            assert get_score(conn, tid) == 20
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Unblocks bonus
# ---------------------------------------------------------------------------

class TestUnblocksBonus:
    def test_one_dependent_adds_five(self, db_path):
        """A task that unblocks 1 other task gets +5."""
        conn = sqlite3.connect(str(db_path))
        try:
            head = insert_task(conn, "head task", priority="Medium", complexity="M")
            dependent = insert_task(conn, "dependent task", priority="Low", complexity="XS")
            # dependent depends_on head → head unblocks dependent
            add_dep(conn, dependent, head, "blocks")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # head: (60 + 10 + 5) / 3 = 25
            assert get_score(conn, head) == 25
        finally:
            conn.close()

    def test_two_dependents_add_ten(self, db_path):
        """A task that unblocks 2 other tasks gets +10."""
        conn = sqlite3.connect(str(db_path))
        try:
            head = insert_task(conn, "head task", priority="Medium", complexity="M")
            for i in range(2):
                dep = insert_task(conn, f"dependent {i}", priority="Low", complexity="XS")
                add_dep(conn, dep, head, "blocks")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # head: (60 + 10 + 10) / 3 = ROUND(26.67) = 27
            assert get_score(conn, head) == 27
        finally:
            conn.close()

    def test_three_or_more_dependents_capped_at_fifteen(self, db_path):
        """Unblocks bonus is capped at 15 regardless of dependent count."""
        conn = sqlite3.connect(str(db_path))
        try:
            head = insert_task(conn, "head task", priority="Medium", complexity="M")
            for i in range(5):
                dep = insert_task(conn, f"dependent {i}", priority="Low", complexity="XS")
                add_dep(conn, dep, head, "blocks")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # head: (60 + 10 + 15) / 3 = ROUND(28.33) = 28
            assert get_score(conn, head) == 28
        finally:
            conn.close()

    def test_contingent_dependent_also_counts_toward_bonus(self, db_path):
        """The unblocks count includes contingent relationship types."""
        conn = sqlite3.connect(str(db_path))
        try:
            head = insert_task(conn, "head task", priority="Medium", complexity="M")
            dep = insert_task(conn, "contingent dependent", priority="Low", complexity="XS")
            # dep depends_on head with contingent type
            add_dep(conn, dep, head, "contingent")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # head unblocks 1 contingent dep → +5; head itself has no deps → no penalty
            # (60 + 10 + 5) / 3 = 25
            assert get_score(conn, head) == 25
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Contingent-only penalty
# ---------------------------------------------------------------------------

class TestContingentOnlyPenalty:
    def test_contingent_only_dep_applies_penalty(self, db_path):
        """A task with only contingent dependencies gets -10."""
        conn = sqlite3.connect(str(db_path))
        try:
            prerequisite = insert_task(conn, "prerequisite task", priority="Low", complexity="XS")
            contingent_task = insert_task(conn, "contingent task", priority="Medium", complexity="M")
            # contingent_task depends_on prerequisite with contingent type
            add_dep(conn, contingent_task, prerequisite, "contingent")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # contingent_task: (60 + 10 + 0 - 10) / 3 = ROUND(20.0) = 20
            assert get_score(conn, contingent_task) == 20
        finally:
            conn.close()

    def test_mixed_deps_no_contingent_penalty(self, db_path):
        """A task with both blocks and contingent deps does NOT get the -10 penalty."""
        conn = sqlite3.connect(str(db_path))
        try:
            prereq1 = insert_task(conn, "prereq 1", priority="Low", complexity="XS")
            prereq2 = insert_task(conn, "prereq 2", priority="Low", complexity="XS")
            mixed_task = insert_task(conn, "mixed deps task", priority="Medium", complexity="M")
            add_dep(conn, mixed_task, prereq1, "blocks")
            add_dep(conn, mixed_task, prereq2, "contingent")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # mixed_task has both blocks and contingent → no penalty
            # (60 + 10 + 0 + 0) / 3 = 23
            assert get_score(conn, mixed_task) == 23
        finally:
            conn.close()

    def test_no_deps_no_contingent_penalty(self, db_path):
        """A task with no dependencies does not get the -10 penalty."""
        conn = sqlite3.connect(str(db_path))
        try:
            tid = insert_task(conn, "no deps task", priority="Medium", complexity="M")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # (60 + 10 + 0 + 0) / 3 = 23
            assert get_score(conn, tid) == 23
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Done tasks excluded
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ELSE branch defaults (unknown priority / unknown complexity)
# ---------------------------------------------------------------------------

class TestElseBranchDefaults:
    def _drop_validation_triggers(self, conn: sqlite3.Connection) -> None:
        """Drop priority and complexity validation triggers so we can insert invalid values."""
        for trigger in (
            "validate_priority_insert",
            "validate_priority_update",
            "validate_complexity_insert",
            "validate_complexity_update",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.commit()

    def test_unknown_priority_defaults_to_40(self, db_path):
        """An unrecognised priority value falls through to ELSE → base 40 (same as Low).

        Formula: ROUND((40 + 10) / 3) = ROUND(16.67) = 17
        Using complexity=M (weight=3) and is_deferred=0 (+10 bonus).
        """
        conn = sqlite3.connect(str(db_path))
        try:
            self._drop_validation_triggers(conn)
            tid = insert_task(conn, "unknown priority task", priority="NonExistent", complexity="M")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            assert get_score(conn, tid) == 17
        finally:
            conn.close()

    def test_unknown_complexity_defaults_to_weight_3(self, db_path):
        """An unrecognised complexity value falls through to ELSE → weight 3 (same as M).

        Formula: ROUND((100 + 10) / 3) = ROUND(36.67) = 37
        Using priority=Highest (100) and is_deferred=0 (+10 bonus).
        Score would be 110 for XS, 55 for S, 22 for L, 14 for XL — 37 confirms weight=3.
        """
        conn = sqlite3.connect(str(db_path))
        try:
            self._drop_validation_triggers(conn)
            tid = insert_task(conn, "unknown complexity task", priority="Highest", complexity="MEGA")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            assert get_score(conn, tid) == 37
        finally:
            conn.close()

    def test_unknown_priority_and_complexity_both_use_defaults(self, db_path):
        """Both unknown priority (→40) and unknown complexity (→weight 3) apply together.

        Formula: ROUND((40 + 10) / 3) = ROUND(16.67) = 17
        """
        conn = sqlite3.connect(str(db_path))
        try:
            self._drop_validation_triggers(conn)
            tid = insert_task(conn, "double unknown task", priority="???", complexity="???")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            assert get_score(conn, tid) == 17
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Done tasks excluded
# ---------------------------------------------------------------------------

class TestDoneTasksExcluded:
    def test_done_tasks_are_not_updated(self, db_path):
        """Tasks with status='Done' are not updated by wsjf."""
        conn = sqlite3.connect(str(db_path))
        try:
            tid = insert_task(conn, "done task", priority="Highest", complexity="XS", status="Done")
            # Manually set a known stale score so we can confirm it's untouched
            conn.execute(
            "UPDATE tasks SET priority_score = 999, closed_reason = 'completed' WHERE id = ?",
            (tid,),
        )
            conn.commit()
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            assert get_score(conn, tid) == 999
        finally:
            conn.close()

    def test_in_progress_tasks_are_scored(self, db_path):
        """Tasks with status='In Progress' ARE updated by wsjf (WHERE status <> 'Done')."""
        conn = sqlite3.connect(str(db_path))
        try:
            tid = insert_task(conn, "active task", priority="Medium", complexity="M", status="In Progress")
        finally:
            conn.close()

        run_wsjf(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # (60 + 10) / 3 = 23
            assert get_score(conn, tid) == 23
        finally:
            conn.close()
