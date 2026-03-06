"""Unit tests for tusk-dupes.py similarity functions.

Covers normalize_summary, tokenize, char_similarity, token_similarity,
and combined_similarity, including threshold boundary cases.
"""

import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.default.json")

# Load the module (hyphenated filename requires importlib)
_spec = importlib.util.spec_from_file_location(
    "tusk_dupes",
    os.path.join(REPO_ROOT, "bin", "tusk-dupes.py"),
)
dupes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dupes)

# Initialize module-level globals (PREFIX_PATTERN, thresholds, TERMINAL_STATUS)
dupes.load_config(CONFIG_PATH)


# ── normalize_summary ─────────────────────────────────────────────────


class TestNormalizeSummary:
    def test_strips_deferred_prefix(self):
        assert dupes.normalize_summary("[Deferred] add tests") == "add tests"

    def test_strips_enhancement_prefix(self):
        assert dupes.normalize_summary("[Enhancement] improve logging") == "improve logging"

    def test_strips_optional_prefix(self):
        assert dupes.normalize_summary("[Optional] clean up comments") == "clean up comments"

    def test_strips_jira_style_prefix(self):
        assert dupes.normalize_summary("[PROJ-123] fix bug") == "fix bug"

    def test_strips_multiple_prefixes(self):
        result = dupes.normalize_summary("[Deferred][Enhancement] refactor init")
        assert result == "refactor init"

    def test_case_folding(self):
        assert dupes.normalize_summary("Fix Bug In Tusk Init") == "fix bug in tusk init"

    def test_whitespace_collapse(self):
        assert dupes.normalize_summary("fix  multiple   spaces") == "fix multiple spaces"

    def test_leading_trailing_whitespace(self):
        assert dupes.normalize_summary("  add logging  ") == "add logging"

    def test_no_prefix_passthrough(self):
        assert dupes.normalize_summary("add unit tests for similarity") == "add unit tests for similarity"

    def test_prefix_case_insensitive(self):
        assert dupes.normalize_summary("[deferred] lowercase prefix") == "lowercase prefix"


# ── tokenize ─────────────────────────────────────────────────────────


class TestTokenize:
    def test_simple_words(self):
        assert dupes.tokenize("fix bug in tusk") == {"fix", "bug", "in", "tusk"}

    def test_compound_token_slash_preserved(self):
        # bin/tusk should remain as a single token (split on whitespace only)
        tokens = dupes.tokenize("update bin/tusk script")
        assert "bin/tusk" in tokens
        assert len(tokens) == 3

    def test_compound_token_hyphen_preserved(self):
        tokens = dupes.tokenize("edit tusk-dupes.py")
        assert "tusk-dupes.py" in tokens

    def test_leading_slash_stripped(self):
        # /tusk and tusk should be the same token
        tokens = dupes.tokenize("/tusk command")
        assert "tusk" in tokens
        assert "/tusk" not in tokens

    def test_leading_slash_does_not_split_compound(self):
        # /bin/tusk → bin/tusk after lstrip("/")
        tokens = dupes.tokenize("/bin/tusk help")
        assert "bin/tusk" in tokens

    def test_empty_string_returns_empty_set(self):
        assert dupes.tokenize("") == set()

    def test_single_word(self):
        assert dupes.tokenize("migrate") == {"migrate"}


# ── char_similarity ───────────────────────────────────────────────────


class TestCharSimilarity:
    def test_identical_strings_return_one(self):
        assert dupes.char_similarity("fix bug in init", "fix bug in init") == pytest.approx(1.0)

    def test_both_empty_return_one(self):
        # Two empty strings are identical
        assert dupes.char_similarity("", "") == pytest.approx(1.0)

    def test_empty_vs_nonempty_return_zero(self):
        assert dupes.char_similarity("", "something") == pytest.approx(0.0)
        assert dupes.char_similarity("something", "") == pytest.approx(0.0)

    def test_completely_different_strings_are_low(self):
        score = dupes.char_similarity("aaaaaa", "zzzzzz")
        assert score < 0.5

    def test_high_similarity_for_near_identical(self):
        # One character different
        score = dupes.char_similarity("fix bug in tusk", "fix bug in tusK")
        assert score > 0.9


# ── token_similarity ──────────────────────────────────────────────────


class TestTokenSimilarity:
    def test_both_empty_return_zero(self):
        # Special case: both token sets empty → 0.0
        assert dupes.token_similarity("", "") == pytest.approx(0.0)

    def test_identical_strings_return_one(self):
        assert dupes.token_similarity("add tests for init", "add tests for init") == pytest.approx(1.0)

    def test_no_overlap_returns_zero(self):
        assert dupes.token_similarity("alpha beta", "gamma delta") == pytest.approx(0.0)

    def test_jaccard_half_overlap(self):
        # {"a", "b"} vs {"b", "c"} → intersection={"b"}, union={"a","b","c"} → 1/3
        score = dupes.token_similarity("a b", "b c")
        assert score == pytest.approx(1 / 3)

    def test_jaccard_full_subset(self):
        # {"a", "b"} vs {"a", "b", "c"} → intersection=2, union=3 → 2/3
        score = dupes.token_similarity("a b", "a b c")
        assert score == pytest.approx(2 / 3)

    def test_one_empty_returns_zero(self):
        assert dupes.token_similarity("add tests", "") == pytest.approx(0.0)
        assert dupes.token_similarity("", "add tests") == pytest.approx(0.0)


# ── combined_similarity ───────────────────────────────────────────────


class TestCombinedSimilarity:
    def test_identical_strings_return_one(self):
        assert dupes.combined_similarity("add unit tests", "add unit tests") == pytest.approx(1.0)

    def test_weights_sum_to_one(self):
        assert dupes.CHAR_WEIGHT + dupes.TOKEN_WEIGHT == pytest.approx(1.0)

    def test_known_near_duplicate_exceeds_threshold(self):
        # Strings that differ only by a stop-word: well above the 0.82 threshold.
        a = "add unit tests for the similarity module"
        b = "add unit tests for similarity module"
        score = dupes.combined_similarity(a, b)
        assert score >= dupes.DEFAULT_CHECK_THRESHOLD, (
            f"Expected near-duplicate score >= {dupes.DEFAULT_CHECK_THRESHOLD}, got {score:.4f}"
        )

    def test_clearly_different_pair_below_threshold(self):
        # Two completely unrelated tasks: should score well below 0.82.
        a = "add unit tests for similarity functions"
        b = "initialise database schema migration tables"
        score = dupes.combined_similarity(a, b)
        assert score < dupes.DEFAULT_CHECK_THRESHOLD, (
            f"Expected non-duplicate score < {dupes.DEFAULT_CHECK_THRESHOLD}, got {score:.4f}"
        )

    def test_formula_matches_manual_calculation(self):
        # Verify the blended formula: CHAR_WEIGHT * char + TOKEN_WEIGHT * token
        a = "fix bug in tusk"
        b = "fix issue in tusk"
        char_s = dupes.char_similarity(a, b)
        token_s = dupes.token_similarity(a, b)
        expected = dupes.CHAR_WEIGHT * char_s + dupes.TOKEN_WEIGHT * token_s
        assert dupes.combined_similarity(a, b) == pytest.approx(expected)
