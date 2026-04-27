"""Unit tests for tusk-dupes.py similarity functions.

Covers normalize_summary, tokenize, char_similarity, token_similarity,
and combined_similarity, including threshold boundary cases. Also covers
the in-progress-criterion match path added in TASK-230 (Issue #603).
"""

import argparse
import importlib.util
import io
import json
import os
import sqlite3
from contextlib import redirect_stdout

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
        assert tokens == {"update", "bin/tusk", "script"}

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
        a = dupes.normalize_summary("add unit tests for the similarity module")
        b = dupes.normalize_summary("add unit tests for similarity module")
        score = dupes.combined_similarity(a, b)
        assert score >= dupes.DEFAULT_CHECK_THRESHOLD, (
            f"Expected near-duplicate score >= {dupes.DEFAULT_CHECK_THRESHOLD}, got {score:.4f}"
        )

    def test_clearly_different_pair_below_threshold(self):
        # Two completely unrelated tasks: should score well below 0.82.
        a = dupes.normalize_summary("add unit tests for similarity functions")
        b = dupes.normalize_summary("initialise database schema migration tables")
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


# ── similarity (public entry point) ──────────────────────────────────


class TestSimilarity:
    def test_identical_raw_strings_return_one(self):
        assert dupes.similarity("Fix Bug In Tusk", "Fix Bug In Tusk") == pytest.approx(1.0)

    def test_strips_prefixes_before_comparing(self):
        # [Deferred] prefix should be normalized away so score equals un-prefixed pair
        score_with_prefix = dupes.similarity("[Deferred] add unit tests", "add unit tests")
        score_without = dupes.similarity("add unit tests", "add unit tests")
        assert score_with_prefix == pytest.approx(score_without)

    def test_near_duplicate_raw_strings_exceed_threshold(self):
        # End-to-end: raw strings go through normalize_summary then combined_similarity
        score = dupes.similarity(
            "add unit tests for the similarity module",
            "add unit tests for similarity module",
        )
        assert score >= dupes.DEFAULT_CHECK_THRESHOLD


# ── In-progress-criterion match path (Issue #603) ─────────────────────


def _make_dupes_db(tasks, criteria):
    """Build an in-memory DB with the minimal schema cmd_check queries.

    tasks: list of (id, summary, status, domain) tuples.
    criteria: list of (id, task_id, criterion, is_completed) tuples.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        " id INTEGER PRIMARY KEY, summary TEXT, status TEXT,"
        " domain TEXT, priority TEXT DEFAULT 'Medium')"
    )
    conn.execute(
        "CREATE TABLE acceptance_criteria ("
        " id INTEGER PRIMARY KEY, task_id INTEGER, criterion TEXT,"
        " is_completed INTEGER DEFAULT 0)"
    )
    for tid, summary, status, domain in tasks:
        conn.execute(
            "INSERT INTO tasks (id, summary, status, domain) VALUES (?, ?, ?, ?)",
            (tid, summary, status, domain),
        )
    for cid, tid, text, done in criteria:
        conn.execute(
            "INSERT INTO acceptance_criteria (id, task_id, criterion, is_completed)"
            " VALUES (?, ?, ?, ?)",
            (cid, tid, text, done),
        )
    conn.commit()
    return conn


def _run_cmd_check(monkeypatch, conn, summary, *, json_out=True,
                   threshold=None, criterion_threshold=None, domain=None):
    """Invoke dupes.cmd_check against an in-memory connection and return parsed JSON."""
    monkeypatch.setattr(dupes, "get_connection", lambda _path: conn)
    args = argparse.Namespace(
        summary=summary,
        domain=domain,
        threshold=dupes.DEFAULT_CHECK_THRESHOLD if threshold is None else threshold,
        criterion_threshold=(
            dupes.DEFAULT_CRITERION_CHECK_THRESHOLD
            if criterion_threshold is None else criterion_threshold
        ),
        json=json_out,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dupes.cmd_check(args, db_path="ignored-by-monkeypatch")
    return rc, json.loads(buf.getvalue()) if json_out else buf.getvalue()


class TestInProgressCriterionMatch:
    """Issue #603: cmd_check must surface near-matches against open criteria
    of In-Progress tasks, not just task summaries.
    """

    def test_repro_scenario_matches_in_progress_criterion(self, monkeypatch):
        """The exact repro from Issue #603: in-progress parent + open criterion,
        proposed summary identical to the criterion text → exit 1, criterion match.
        """
        conn = _make_dupes_db(
            tasks=[(70, "Wire up the iOS app's networking spine", "In Progress", "ios")],
            criteria=[
                (243, 70,
                 "Add iOS networking layer with URLSession and Bearer auth", 0),
            ],
        )
        rc, payload = _run_cmd_check(
            monkeypatch, conn,
            "Add iOS networking layer with URLSession and Bearer auth",
        )
        assert rc == 1
        assert len(payload["duplicates"]) == 1
        match = payload["duplicates"][0]
        assert match["match_type"] == "criterion"
        assert match["id"] == 70
        assert match["criterion_id"] == 243
        assert match["criterion"] == (
            "Add iOS networking layer with URLSession and Bearer auth"
        )
        assert match["similarity"] >= dupes.DEFAULT_CRITERION_CHECK_THRESHOLD

    def test_completed_criterion_is_not_matched(self, monkeypatch):
        """Completed criteria represent already-shipped work — surfacing them as
        duplicates of follow-up tasks would be a false positive (Issue #603 fix
        scope: incomplete criteria only)."""
        conn = _make_dupes_db(
            tasks=[(70, "Some unrelated parent", "In Progress", None)],
            criteria=[
                (243, 70,
                 "Add iOS networking layer with URLSession and Bearer auth", 1),
            ],
        )
        rc, payload = _run_cmd_check(
            monkeypatch, conn,
            "Add iOS networking layer with URLSession and Bearer auth",
        )
        assert rc == 0
        assert payload["duplicates"] == []

    def test_done_task_criterion_is_not_matched(self, monkeypatch):
        """Criteria of Done tasks are out of scope — only In-Progress tasks
        represent active duplicate-of-work risk."""
        conn = _make_dupes_db(
            tasks=[(70, "Some shipped parent", "Done", None)],
            criteria=[
                (243, 70,
                 "Add iOS networking layer with URLSession and Bearer auth", 0),
            ],
        )
        rc, payload = _run_cmd_check(
            monkeypatch, conn,
            "Add iOS networking layer with URLSession and Bearer auth",
        )
        assert rc == 0
        assert payload["duplicates"] == []

    def test_criterion_below_stricter_threshold_is_not_matched(self, monkeypatch):
        """The criterion threshold gates which criterion-text matches surface.
        A score above the summary threshold but below the criterion threshold
        is rejected — that gap is what protects against broad-scope criteria
        false-matching narrow proposed summaries."""
        proposed = "add unit tests for the similarity module"
        criterion_text = "add unit tests for similarity module"
        # Pin the score (used in the existing TestCombinedSimilarity case) and
        # bracket criterion_threshold strictly above it so the gating is
        # provable regardless of future tuning of the default thresholds.
        score = dupes.similarity(proposed, criterion_text)
        criterion_threshold = score + 0.01
        assert score < criterion_threshold

        # cmd_check closes its connection in a finally — each invocation
        # needs its own fresh DB.
        def fresh_db():
            return _make_dupes_db(
                tasks=[(70, "Some unrelated parent summary", "In Progress", None)],
                criteria=[(243, 70, criterion_text, 0)],
            )

        rc, payload = _run_cmd_check(
            monkeypatch, fresh_db(), proposed,
            criterion_threshold=criterion_threshold,
        )
        assert rc == 0
        assert payload["duplicates"] == []

        # And confirm that lowering the threshold below the score does match.
        rc2, payload2 = _run_cmd_check(
            monkeypatch, fresh_db(), proposed,
            criterion_threshold=score - 0.01,
        )
        assert rc2 == 1
        assert payload2["duplicates"][0]["match_type"] == "criterion"

    def test_summary_and_criterion_match_does_not_double_report(self, monkeypatch):
        """When the parent task already matches on summary, its criteria are
        skipped to avoid surfacing the same task twice."""
        conn = _make_dupes_db(
            tasks=[(70,
                    "Add iOS networking layer with URLSession and Bearer auth",
                    "In Progress", None)],
            criteria=[
                (243, 70,
                 "Add iOS networking layer with URLSession and Bearer auth", 0),
            ],
        )
        rc, payload = _run_cmd_check(
            monkeypatch, conn,
            "Add iOS networking layer with URLSession and Bearer auth",
        )
        assert rc == 1
        assert len(payload["duplicates"]) == 1
        # The single match is the summary one, not the criterion one.
        assert payload["duplicates"][0]["match_type"] == "summary"

    def test_multiple_open_criteria_on_one_task_yield_one_match(self, monkeypatch):
        """One in-progress task with N open criteria all clearing the threshold
        must surface as exactly one duplicate entry — the highest-scoring
        criterion. Without this guard, a task with 5 broad criteria all
        matching a proposed summary would emit 5 duplicate rows pointing at
        the same task, swamping /create-task semantic-dedup output."""
        # Both criteria match the proposed input; #244 is a closer textual
        # match (one extra word), so it should win the per-task tie.
        proposed = "Add iOS networking layer with URLSession and Bearer auth"
        weaker = "Add iOS networking layer with URLSession Bearer auth"
        stronger = "Add iOS networking layer with URLSession and Bearer auth"

        # Pre-condition: both clear the criterion threshold (otherwise the
        # test devolves to the single-match case and proves nothing).
        assert dupes.similarity(proposed, weaker) >= dupes.DEFAULT_CRITERION_CHECK_THRESHOLD
        assert dupes.similarity(proposed, stronger) >= dupes.DEFAULT_CRITERION_CHECK_THRESHOLD

        conn = _make_dupes_db(
            tasks=[(70, "iOS networking spine", "In Progress", None)],
            criteria=[
                (243, 70, weaker, 0),
                (244, 70, stronger, 0),
            ],
        )
        rc, payload = _run_cmd_check(monkeypatch, conn, proposed)
        assert rc == 1
        assert len(payload["duplicates"]) == 1
        match = payload["duplicates"][0]
        assert match["match_type"] == "criterion"
        assert match["id"] == 70
        # Highest-scoring criterion (#244, the verbatim match) wins the tie.
        assert match["criterion_id"] == 244
        assert match["criterion"] == stronger

    def test_summary_only_matches_keep_existing_shape(self, monkeypatch):
        """Regression guard: existing summary-only matches gain match_type
        but otherwise keep their JSON shape (id, summary, domain, similarity)."""
        conn = _make_dupes_db(
            tasks=[(50, "Add unit tests for similarity module", "To Do", "cli")],
            criteria=[],
        )
        rc, payload = _run_cmd_check(
            monkeypatch, conn,
            "Add unit tests for similarity module",
        )
        assert rc == 1
        match = payload["duplicates"][0]
        assert match == {
            "id": 50,
            "summary": "Add unit tests for similarity module",
            "domain": "cli",
            "similarity": pytest.approx(1.0),
            "match_type": "summary",
        }


class TestCriterionThresholdConfig:
    def test_default_criterion_threshold_loaded_from_config(self):
        """Module global is sourced from config.dupes.criterion_check_threshold."""
        assert dupes.DEFAULT_CRITERION_CHECK_THRESHOLD == 0.88

    def test_criterion_threshold_is_stricter_than_summary_threshold(self):
        """Per-issue rationale: criteria are usually broader-scope text than
        summaries, so a higher threshold is needed to keep false positives down."""
        assert (
            dupes.DEFAULT_CRITERION_CHECK_THRESHOLD
            > dupes.DEFAULT_CHECK_THRESHOLD
        )


class TestInProgressStatusResolution:
    """load_config must resolve IN_PROGRESS_STATUS by name match, not by index.
    The earlier statuses[1] heuristic broke for taxonomies that prepend a
    stage (review finding #60)."""

    def _write_and_load(self, tmp_path, payload):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(payload))
        dupes.load_config(str(cfg_path))
        return dupes.IN_PROGRESS_STATUS

    def teardown_method(self):
        # Reset module globals back to the canonical config so other tests
        # in this module don't see the test-local config bleed through.
        dupes.load_config(CONFIG_PATH)

    def test_default_three_state_taxonomy(self, tmp_path):
        result = self._write_and_load(
            tmp_path, {"statuses": ["To Do", "In Progress", "Done"]}
        )
        assert result == "In Progress"

    def test_four_state_taxonomy_with_prepended_stage(self, tmp_path):
        """Reviewer's example: a four-state list that prepends Backlog
        used to incorrectly resolve to 'To Do' under statuses[1]."""
        result = self._write_and_load(
            tmp_path, {"statuses": ["Backlog", "To Do", "In Progress", "Done"]}
        )
        assert result == "In Progress"

    def test_explicit_in_progress_status_config_wins(self, tmp_path):
        """An explicit dupes.in_progress_status overrides the name match —
        useful for projects that rename the active stage entirely."""
        result = self._write_and_load(
            tmp_path,
            {
                "statuses": ["queued", "active", "shipped"],
                "dupes": {"in_progress_status": "active"},
            },
        )
        assert result == "active"

    def test_falls_back_to_literal_when_no_name_match(self, tmp_path):
        """When the statuses list has no entry literally named 'In Progress'
        and no explicit override, fall back to the literal 'In Progress'.
        This preserves the historical contract (the schema invariant)."""
        result = self._write_and_load(
            tmp_path, {"statuses": ["queued", "active", "shipped"]}
        )
        assert result == "In Progress"

    def test_name_match_is_case_insensitive(self, tmp_path):
        """Status taxonomy capitalisation is project-defined; match by
        canonical lowercase form so 'in progress' / 'IN PROGRESS' both bind."""
        result = self._write_and_load(
            tmp_path, {"statuses": ["To Do", "in progress", "Done"]}
        )
        assert result == "in progress"
