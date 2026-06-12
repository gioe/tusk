"""Unit tests for claude-fable-5 pricing and the unknown-model cost warning.

Covers GitHub issues #1036/#1038/#1039/#1060/#1064: the pricing table had no
entry for the claude-fable-5 family, and compute_cost() silently returned 0.0
(debug-level log only) for unpriced models with recorded token counts.
"""

import importlib.util
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_pricing_lib",
    os.path.join(BIN, "tusk-pricing-lib.py"),
)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)


def _totals(model, **overrides):
    totals = {
        "model": model,
        "input_tokens": 0,
        "cache_creation_5m_tokens": 0,
        "cache_creation_1h_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    totals.update(overrides)
    return totals


@pytest.fixture(autouse=True)
def _fresh_pricing():
    lib.load_pricing()
    lib._WARNED_UNPRICED_MODELS.clear()
    yield


class TestFablePricing:
    def test_pricing_table_has_fable_entry(self):
        rates = lib.PRICING["claude-fable-5"]
        assert rates == {
            "input": 10.0,
            "cache_write_5m": 12.5,
            "cache_write_1h": 20.0,
            "cache_read": 1.0,
            "output": 50.0,
        }

    def test_pricing_table_has_mythos_and_opus_4_8_entries(self):
        assert lib.PRICING["claude-mythos-5"] == lib.PRICING["claude-fable-5"]
        assert lib.PRICING["claude-opus-4-8"]["input"] == 5.0
        assert lib.PRICING["claude-opus-4-8"]["output"] == 25.0

    def test_resolve_model_bare_id(self):
        assert lib.resolve_model("claude-fable-5") == "claude-fable-5"

    def test_resolve_model_bracketed_context_suffix(self):
        # Claude Code reports the session model as claude-fable-5[1m]; the
        # prefix match in resolve_model must map it onto the pricing key.
        assert lib.resolve_model("claude-fable-5[1m]") == "claude-fable-5"

    def test_compute_cost_nonzero_for_fable(self):
        totals = _totals(
            "claude-fable-5",
            input_tokens=1_000_000,
            cache_read_input_tokens=2_000_000,
            output_tokens=100_000,
        )
        # 1M * $10 + 2M * $1 + 0.1M * $50 = 10 + 2 + 5
        assert lib.compute_cost(totals) == 17.0

    def test_context_window_is_one_million(self):
        assert lib.get_context_window("claude-fable-5") == 1_000_000
        assert lib.get_context_window("claude-opus-4-8") == 1_000_000


class TestUnknownModelWarning:
    def test_warns_on_stderr_when_tokens_nonzero(self, capsys):
        totals = _totals("claude-future-9", input_tokens=5_000_000, output_tokens=10_000)
        assert lib.compute_cost(totals) == 0.0
        err = capsys.readouterr().err
        assert "claude-future-9" in err
        assert "pricing" in err

    def test_no_warning_when_tokens_zero(self, capsys):
        assert lib.compute_cost(_totals("claude-future-9")) == 0.0
        assert capsys.readouterr().err == ""

    def test_no_warning_for_priced_model(self, capsys):
        totals = _totals("claude-fable-5", input_tokens=1_000)
        assert lib.compute_cost(totals) > 0.0
        assert capsys.readouterr().err == ""

    def test_warning_emitted_once_per_model(self, capsys):
        totals = _totals("claude-future-9", input_tokens=1_000)
        lib.compute_cost(totals)
        lib.compute_cost(totals)
        lib.compute_cost(_totals("claude-other-1", input_tokens=1_000))
        err = capsys.readouterr().err
        assert err.count("claude-future-9") == 1
        assert err.count("claude-other-1") == 1
