"""Unit tests for task-start's progressive spec-rigor advisory."""

import importlib.util
import os
import sqlite3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-start.py")


def _load():
    spec = importlib.util.spec_from_file_location("tusk_task_start", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _conn(*, complexity, criterion_specs=None, criteria=None, objective=False, context_types=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            description TEXT,
            complexity TEXT
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            criterion TEXT,
            criterion_type TEXT,
            verification_spec TEXT,
            is_deferred INTEGER DEFAULT 0
        );
        CREATE TABLE objective_tasks (
            objective_id INTEGER,
            task_id INTEGER,
            relationship_type TEXT
        );
        CREATE TABLE task_context_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            item_type TEXT,
            content TEXT,
            status TEXT DEFAULT 'active'
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, summary, description, complexity) VALUES (1, 'task', 'desc', ?)",
        (complexity,),
    )
    criterion_rows = criteria
    if criterion_rows is None:
        criterion_rows = [("criterion", spec) for spec in (criterion_specs or [None])]
    for criterion, spec in criterion_rows:
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, criterion_type, verification_spec) "
            "VALUES (1, ?, ?, ?)",
            (criterion, "test" if spec else "manual", spec),
        )
    if objective:
        conn.execute(
            "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) "
            "VALUES (7, 1, 'primary')"
        )
    for item_type in context_types or []:
        conn.execute(
            "INSERT INTO task_context_items (task_id, item_type, content, status) "
            "VALUES (1, ?, 'context', 'active')",
            (item_type,),
        )
    conn.commit()
    return conn


def test_xs_and_s_tasks_do_not_require_extra_spec_ceremony():
    mod = _load()

    assert mod._spec_rigor_advisory_lines(_conn(complexity="XS"), 1) == []
    assert mod._spec_rigor_advisory_lines(_conn(complexity="S"), 1) == []


def test_m_task_warns_when_no_criteria_have_verification_specs():
    mod = _load()

    lines = mod._spec_rigor_advisory_lines(_conn(complexity="M"), 1)

    assert any("M task has no verification-backed criteria" in line for line in lines)
    assert not any("objective" in line for line in lines)


def test_m_task_warns_when_criteria_are_too_vague_to_observe():
    mod = _load()

    lines = mod._spec_rigor_advisory_lines(
        _conn(complexity="M", criteria=[("Improve task readiness", None)]),
        1,
    )

    assert any("vague acceptance criteria" in line for line in lines)
    assert any("Improve task readiness" in line for line in lines)


def test_l_task_warns_for_missing_objective_context_and_verification():
    mod = _load()

    lines = mod._spec_rigor_advisory_lines(_conn(complexity="L"), 1)

    assert any("L task is not linked to an objective" in line for line in lines)
    assert any("L task has no active risk, assumption, or decision context" in line for line in lines)
    assert any("L task has no verification-backed criteria" in line for line in lines)


def test_l_task_with_objective_context_and_verification_has_no_warnings():
    mod = _load()

    lines = mod._spec_rigor_advisory_lines(
        _conn(
            complexity="L",
            criterion_specs=["python3 -m pytest tests/unit/test_example.py -q"],
            objective=True,
            context_types=["risk"],
        ),
        1,
    )

    assert lines == []
