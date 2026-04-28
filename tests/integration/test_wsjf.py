"""Integration tests for WSJF scoring (bin/tusk wsjf).

Uses the db_path fixture (a real initialised SQLite DB), inserts tasks with
known priority/complexity combinations, calls `bin/tusk wsjf` via subprocess,
then queries priority_score and asserts exact expected values.

Formula (from cmd_wsjf in bin/tusk):
  priority_score = ROUND(
    (base_priority + unblocks_bonus + contingent_penalty)
    / complexity_weight
  )

  base_priority    : Highest=100, High=80, Medium=60, Low=40, Lowest=20
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
    """Call `bin/tusk wsjf` against the given database.

    TUSK_DB is pinned by the db_path fixture via monkeypatch, so subprocess
    calls inherit it without the caller threading env overrides.
    """
    result = subprocess.run(
        [TUSK_BIN, "wsjf"],
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
) -> int:
    """Insert a task and return its id."""
    cur = conn.execute(
        """
        INSERT INTO tasks (summary, status, priority, complexity, task_type, priority_score)
        VALUES (?, ?, ?, ?, 'feature', 0)
        """,
        (summary, status, priority, complexity),
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
# Parameterised: priority × complexity (8 cases, no deps)
# ---------------------------------------------------------------------------

# (priority, complexity, expected_score)
# score = ROUND(base / weight)
PRIORITY_COMPLEXITY_CASES = [
    ("Highest", "XS", 100),   # ROUND(100/1) = 100
    ("High",    "S",   40),   # ROUND(80/2)  = 40
    ("Medium",  "M",   20),   # ROUND(60/3)  = 20
    ("Low",     "L",    8),   # ROUND(40/5)  = 8
    ("Lowest",  "XL",   3),   # ROUND(20/8)  = 3 (2.5→3 banker's? sqlite ROUND → 3)
    ("Medium",  "XS",  60),   # ROUND(60/1)  = 60
    ("High",    "XL",  10),   # ROUND(80/8)  = 10
    ("Low",     "S",   20),   # ROUND(40/2)  = 20
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
            # head: (60 + 5) / 3 = ROUND(21.67) = 22
            assert get_score(conn, head) == 22
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
            # head: (60 + 10) / 3 = ROUND(23.33) = 23
            assert get_score(conn, head) == 23
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
            # head: (60 + 15) / 3 = 25
            assert get_score(conn, head) == 25
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
            # (60 + 5) / 3 = ROUND(21.67) = 22
            assert get_score(conn, head) == 22
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
            # contingent_task: (60 + 0 - 10) / 3 = ROUND(16.67) = 17
            assert get_score(conn, contingent_task) == 17
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
            # (60 + 0 + 0) / 3 = 20
            assert get_score(conn, mixed_task) == 20
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
            # (60 + 0 + 0) / 3 = 20
            assert get_score(conn, tid) == 20
        finally:
            conn.close()


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

        Formula: ROUND(40 / 3) = ROUND(13.33) = 13
        Using complexity=M (weight=3).
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
            assert get_score(conn, tid) == 13
        finally:
            conn.close()

    def test_unknown_complexity_defaults_to_weight_3(self, db_path):
        """An unrecognised complexity value falls through to ELSE → weight 3 (same as M).

        Formula: ROUND(100 / 3) = ROUND(33.33) = 33
        Using priority=Highest (100).
        Score would be 100 for XS, 50 for S, 20 for L, 13 for XL — 33 confirms weight=3.
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
            assert get_score(conn, tid) == 33
        finally:
            conn.close()

    def test_unknown_priority_and_complexity_both_use_defaults(self, db_path):
        """Both unknown priority (→40) and unknown complexity (→weight 3) apply together.

        Formula: ROUND(40 / 3) = ROUND(13.33) = 13

        Note: the expected score (13) is the same as the priority-only-unknown test because
        the complexity ELSE default (weight=3) equals the weight for 'M'. The test still
        exercises the combined code path — both CASE expressions hit their ELSE branch.
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
            assert get_score(conn, tid) == 13
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
            # 60 / 3 = 20
            assert get_score(conn, tid) == 20
        finally:
            conn.close()
