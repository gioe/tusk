"""Unit tests for tusk-plans.py — pure date-range selection helper.

The CLI surface (set/list/end) is covered by tests/integration/test_plans_cli.py;
these tests exercise the pure ``select_active_plans`` function that
issue #871's eventual ROI rollup will consume, with no DB or I/O.
"""

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load_plans():
    sys.path.insert(0, SCRIPT_DIR)
    spec = importlib.util.spec_from_file_location(
        "tusk_plans",
        os.path.join(SCRIPT_DIR, "tusk-plans.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_plans = _load_plans()
select_active_plans = tusk_plans.select_active_plans


def _row(name, monthly_cost, eff_from, eff_to=None):
    return {
        "id": 1,
        "name": name,
        "monthly_cost_dollars": monthly_cost,
        "effective_from": eff_from,
        "effective_to": eff_to,
        "notes": None,
    }


class TestSelectActivePlans:

    def test_open_period_includes_today(self):
        rows = [_row("claude_max", 200.0, "2026-01-01")]
        active = select_active_plans(rows, "2026-05-15")
        assert len(active) == 1
        assert active[0]["name"] == "claude_max"

    def test_open_period_excludes_pre_start_date(self):
        rows = [_row("claude_max", 200.0, "2026-03-01")]
        assert select_active_plans(rows, "2026-02-28") == []

    def test_start_date_is_inclusive(self):
        rows = [_row("claude_max", 200.0, "2026-03-01")]
        assert len(select_active_plans(rows, "2026-03-01")) == 1

    def test_end_date_is_exclusive(self):
        """A plan ending on 2026-04-01 is NOT active on 2026-04-01 itself —
        the replacement plan covers the cutover day."""
        rows = [_row("claude_max", 200.0, "2026-01-01", "2026-04-01")]
        assert select_active_plans(rows, "2026-04-01") == []
        assert len(select_active_plans(rows, "2026-03-31")) == 1

    def test_returns_multiple_active_plans(self):
        """Concurrent subscriptions (e.g. Claude Max + ChatGPT Pro) should
        both surface when active on the same date."""
        rows = [
            _row("claude_max", 200.0, "2026-01-01"),
            _row("chatgpt_pro", 200.0, "2026-02-01"),
        ]
        active = select_active_plans(rows, "2026-05-15")
        names = sorted(r["name"] for r in active)
        assert names == ["chatgpt_pro", "claude_max"]

    def test_filters_out_expired_plan(self):
        rows = [
            _row("old_plan", 100.0, "2025-01-01", "2025-12-31"),
            _row("current", 200.0, "2026-01-01"),
        ]
        active = select_active_plans(rows, "2026-05-15")
        assert [r["name"] for r in active] == ["current"]

    def test_empty_input_returns_empty(self):
        assert select_active_plans([], "2026-05-15") == []

    def test_history_query_at_old_date(self):
        """Asking 'what was active on Jan 15, 2025?' should surface the
        plan that was open then, even if a later replacement now covers."""
        rows = [
            _row("old_plan", 100.0, "2025-01-01", "2025-12-31"),
            _row("current", 200.0, "2026-01-01"),
        ]
        active = select_active_plans(rows, "2025-01-15")
        assert [r["name"] for r in active] == ["old_plan"]
