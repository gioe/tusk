"""Unit tests for tusk-skill-patch-priority.py (TASK-715).

Skill-patch follow-up tasks should land at a priority derived from their
retro-signals (reopen counts, rework chains, review themes) rather than the
unmodified default priority. These tests cover the three criterion node IDs
referenced by the task's verification specs:

    -k computes   → priority is computed from retro-signals
    -k monotonic  → higher reopen/rework counts yield higher priority
    -k applied    → the computed priority is applied to the task
"""

import importlib.util
import os
import sqlite3
import sys

import pytest

BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin",
)
sys.path.insert(0, BIN)


def _load_module():
    path = os.path.join(BIN, "tusk-skill-patch-priority.py")
    spec = importlib.util.spec_from_file_location("tusk_skill_patch_priority", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


spp = _load_module()

DEFAULT_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]


def _signals(reopen=0, fixes=0, fixed_by=0, themes=None):
    return {
        "task_id": 1,
        "reopen_count": reopen,
        "rework_chain": {
            "fixes": [{"id": i} for i in range(fixes)],
            "fixed_by": [{"id": i} for i in range(fixed_by)],
        },
        "review_themes": themes or [],
    }


def _priority_index(priority):
    return DEFAULT_PRIORITIES.index(priority)


# --------------------------------------------------------------------------
# computes — priority is computed from retro-signals (criterion 3340)
# --------------------------------------------------------------------------


def test_computes_returns_a_valid_priority_label():
    p = spp.compute_priority(_signals(), DEFAULT_PRIORITIES)
    assert p in DEFAULT_PRIORITIES


def test_computes_zero_pressure_lands_at_default_not_highest():
    # No rework history → the configured default (middle) priority, NOT inflated.
    p = spp.compute_priority(_signals(), DEFAULT_PRIORITIES)
    assert p == "Medium"
    assert p != "Highest"


def test_computes_pressure_aggregates_all_signal_sources():
    pressure = spp.compute_pressure(
        _signals(
            reopen=2,
            fixes=1,
            fixed_by=1,
            themes=[{"count": 3}],
        )
    )
    assert pressure == 2 + 1 + 1 + 3


def test_computes_nonzero_pressure_lifts_above_default():
    p = spp.compute_priority(_signals(reopen=1), DEFAULT_PRIORITIES)
    assert _priority_index(p) < _priority_index("Medium")  # higher = lower index


def test_computes_handles_missing_signal_keys_gracefully():
    # An empty / partial signals dict must not raise and must yield default.
    assert spp.compute_priority({}, DEFAULT_PRIORITIES) == "Medium"


def test_computes_respects_custom_priority_ladder():
    ladder = ["P0", "P1", "P2", "P3"]
    p = spp.compute_priority(_signals(reopen=10), ladder)
    assert p == "P0"  # saturates at highest


# --------------------------------------------------------------------------
# monotonic — higher reopen and rework counts yield higher priority (3341)
# --------------------------------------------------------------------------


def test_monotonic_more_reopens_never_lower_priority():
    prev_idx = None
    for reopen in range(0, 6):
        p = spp.compute_priority(_signals(reopen=reopen), DEFAULT_PRIORITIES)
        idx = _priority_index(p)
        if prev_idx is not None:
            assert idx <= prev_idx  # lower index == higher priority
        prev_idx = idx


def test_monotonic_more_rework_never_lower_priority():
    prev_idx = None
    for n in range(0, 6):
        p = spp.compute_priority(
            _signals(fixes=n, fixed_by=n), DEFAULT_PRIORITIES
        )
        idx = _priority_index(p)
        if prev_idx is not None:
            assert idx <= prev_idx
        prev_idx = idx


def test_monotonic_higher_reopen_strictly_outranks_zero():
    low = spp.compute_priority(_signals(reopen=0), DEFAULT_PRIORITIES)
    high = spp.compute_priority(_signals(reopen=3), DEFAULT_PRIORITIES)
    assert _priority_index(high) < _priority_index(low)


def test_monotonic_pressure_is_nondecreasing_in_each_input():
    base = spp.compute_pressure(_signals(reopen=1, fixes=1, fixed_by=1))
    assert spp.compute_pressure(_signals(reopen=2, fixes=1, fixed_by=1)) >= base
    assert spp.compute_pressure(_signals(reopen=1, fixes=2, fixed_by=1)) >= base
    assert spp.compute_pressure(_signals(reopen=1, fixes=1, fixed_by=2)) >= base
    assert (
        spp.compute_pressure(
            _signals(reopen=1, fixes=1, fixed_by=1, themes=[{"count": 1}])
        )
        >= base
    )


def test_monotonic_combined_pressure_outranks_single_signal():
    single = spp.compute_priority(_signals(reopen=1), DEFAULT_PRIORITIES)
    combined = spp.compute_priority(
        _signals(reopen=1, fixes=1, fixed_by=1, themes=[{"count": 2}]),
        DEFAULT_PRIORITIES,
    )
    assert _priority_index(combined) <= _priority_index(single)


# --------------------------------------------------------------------------
# applied — the computed priority is applied to the task (criterion 3342)
# --------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, priority TEXT, "
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    yield c
    c.close()


def test_applied_persists_priority_on_task(conn):
    conn.execute("INSERT INTO tasks (id, priority) VALUES (1, 'Medium')")
    conn.commit()
    spp.apply_priority(conn, 1, "High")
    row = conn.execute("SELECT priority FROM tasks WHERE id = 1").fetchone()
    assert row["priority"] == "High"


def test_applied_overwrites_default_priority(conn):
    # Simulate a skill-patch task that landed at default 'Medium'.
    conn.execute("INSERT INTO tasks (id, priority) VALUES (7, 'Medium')")
    conn.commit()
    signals = _signals(reopen=2, fixes=1)
    computed = spp.compute_priority(signals, DEFAULT_PRIORITIES)
    assert computed != "Medium"  # genuinely changes the landing priority
    spp.apply_priority(conn, 7, computed)
    row = conn.execute("SELECT priority FROM tasks WHERE id = 7").fetchone()
    assert row["priority"] == computed


def test_applied_load_priorities_falls_back_when_config_missing():
    assert spp.load_priorities("/nonexistent/config.json") == DEFAULT_PRIORITIES
