"""Regression coverage for gpt-5.6-sol pricing support."""

import importlib.util
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lib = _load("tusk_pricing_lib_gpt56", "tusk-pricing-lib.py")
update = _load("tusk_pricing_update_gpt56", "tusk-pricing-update.py")


def _totals(**overrides):
    totals = {
        "model": "gpt-5.6-sol",
        "input_tokens": 1_000_000,
        "cache_creation_5m_tokens": 0,
        "cache_creation_1h_tokens": 0,
        "cache_read_input_tokens": 2_000_000,
        "output_tokens": 100_000,
        "request_count": 1,
    }
    totals.update(overrides)
    return totals


@pytest.fixture(autouse=True)
def _fresh_pricing():
    lib.load_pricing()
    yield


class TestGpt56SolPricing:
    def test_pricing_table_has_official_standard_rates(self):
        assert lib.PRICING["gpt-5.6-sol"] == {
            "input": 5.0,
            "cache_write_5m": 6.25,
            "cache_write_1h": 6.25,
            "cache_read": 0.5,
            "output": 30.0,
            "context_window": 1_050_000,
        }

    def test_exact_model_lookup_and_context_window(self):
        assert lib.resolve_model("gpt-5.6-sol") == "gpt-5.6-sol"
        assert lib.get_context_window("gpt-5.6-sol") == 1_050_000

    def test_usage_has_known_nonzero_cost(self):
        totals = _totals()

        assert lib.telemetry_status(totals) == "captured"
        assert lib.optional_cost(totals) == 9.0

    def test_existing_claude_pricing_is_unchanged(self):
        assert lib.PRICING["claude-opus-4-6"]["input"] == 5.0
        assert lib.PRICING["claude-opus-4-6"]["output"] == 25.0


def test_pricing_update_preserves_non_claude_models_only():
    old_models = {
        "gpt-5.6-sol": {"input": 5.0, "context_window": 1_050_000},
        "claude-retired": {"input": 99.0},
    }
    new_models = {"claude-current": {"input": 3.0}}

    result = update.preserve_external_models(new_models, old_models)

    assert result == {
        "claude-current": {"input": 3.0},
        "gpt-5.6-sol": {"input": 5.0, "context_window": 1_050_000},
    }


def test_pricing_update_does_not_mask_an_empty_anthropic_scrape():
    old_models = {"gpt-5.6-sol": {"input": 5.0}}

    assert update.preserve_external_models({}, old_models) == {}
