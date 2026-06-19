"""Unit tests for consolidated per-model context windows (GitHub issue #1088).

Context windows are sourced from the per-model ``context_window`` field in
pricing.json. ``get_context_window()`` reads that loaded value first and only
falls back to the hardcoded CONTEXT_WINDOW table (for callers that never ran
load_pricing()) and then CONTEXT_WINDOW_DEFAULT. ``tusk pricing-update`` carries
the field forward across a rate refresh so registering a model stays a
single-file edit.
"""

import importlib.util
import json
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(BIN, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lib = _load("tusk_pricing_lib_ctx", "tusk-pricing-lib.py")
update = _load("tusk_pricing_update_ctx", "tusk-pricing-update.py")


@pytest.fixture(autouse=True)
def _fresh_pricing():
    lib.load_pricing()
    yield


def _pricing_json():
    with open(os.path.join(REPO_ROOT, "pricing.json")) as f:
        return json.load(f)


class TestPricingJsonCarriesContextWindow:
    def test_every_priced_model_has_an_integer_context_window(self):
        models = _pricing_json()["models"]
        missing = [m for m, e in models.items() if not isinstance(e.get("context_window"), int)]
        assert missing == [], f"models missing context_window in pricing.json: {missing}"

    def test_context_window_matches_hardcoded_fallback_table(self):
        # Criterion 1: the pricing.json values must match the literals that
        # remain in the CONTEXT_WINDOW fallback table.
        models = _pricing_json()["models"]
        for model, entry in models.items():
            if model in lib.CONTEXT_WINDOW:
                assert entry["context_window"] == lib.CONTEXT_WINDOW[model], model


class TestGetContextWindow:
    def test_reads_pricing_json_value(self):
        assert lib.get_context_window("claude-opus-4-8") == 1_000_000
        assert lib.get_context_window("claude-haiku-4-5") == 200_000

    def test_pricing_value_wins_over_stale_fallback_table(self):
        # If the loaded pricing entry disagrees with the hardcoded table, the
        # loaded value (single source of truth) is authoritative.
        lib.PRICING["claude-opus-4-8"]["context_window"] = 555_000
        try:
            assert lib.get_context_window("claude-opus-4-8") == 555_000
        finally:
            lib.PRICING["claude-opus-4-8"]["context_window"] = 1_000_000

    def test_new_model_registered_in_pricing_json_alone(self):
        # Criterion 4: a model present only in loaded pricing.json (never added
        # to the CONTEXT_WINDOW table) resolves its window with no code edit.
        assert "claude-zzz-future" not in lib.CONTEXT_WINDOW
        lib.PRICING["claude-zzz-future"] = {"input": 1.0, "context_window": 1_000_000}
        try:
            assert lib.get_context_window("claude-zzz-future") == 1_000_000
        finally:
            del lib.PRICING["claude-zzz-future"]

    def test_falls_back_to_table_when_not_loaded(self):
        # A caller that never ran load_pricing() (empty PRICING) still resolves
        # known models from the hardcoded fallback table.
        saved = lib.PRICING
        lib.PRICING = {}
        try:
            assert lib.get_context_window("claude-opus-4-8") == 1_000_000
        finally:
            lib.PRICING = saved

    def test_falls_back_to_table_when_entry_lacks_field(self):
        lib.PRICING["claude-opus-4-8"].pop("context_window")
        try:
            assert lib.get_context_window("claude-opus-4-8") == 1_000_000  # from table
        finally:
            lib.PRICING["claude-opus-4-8"]["context_window"] = 1_000_000

    def test_absent_model_returns_default(self):
        saved = lib.PRICING
        lib.PRICING = {}
        try:
            assert lib.get_context_window("claude-unknown-xyz") == lib.CONTEXT_WINDOW_DEFAULT
            assert lib.CONTEXT_WINDOW_DEFAULT == 200_000
        finally:
            lib.PRICING = saved


class TestPricingUpdatePreservesContextWindow:
    def test_carries_forward_surviving_model_window(self):
        old = {"claude-opus-4-8": {"input": 5.0, "context_window": 1_000_000}}
        new = {"claude-opus-4-8": {"input": 5.0}}
        result = update.preserve_context_windows(new, old)
        assert result["claude-opus-4-8"]["context_window"] == 1_000_000

    def test_leaves_fresh_model_without_prior_window_untouched(self):
        old = {}
        new = {"claude-brand-new": {"input": 2.0}}
        result = update.preserve_context_windows(new, old)
        assert "context_window" not in result["claude-brand-new"]

    def test_dropped_model_does_not_leak_into_new_models(self):
        old = {"claude-retired": {"input": 9.0, "context_window": 200_000}}
        new = {"claude-opus-4-8": {"input": 5.0}}
        result = update.preserve_context_windows(new, old)
        assert "claude-retired" not in result
