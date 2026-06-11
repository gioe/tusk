"""Unit coverage for counterfactual path-mention filtering (issue #1071).

A path the description explicitly marks as nonexistent — "(does not exist)",
"(deleted)", "(removed in TASK-12)", or a leading not/never in the same
clause — describes a path the task will NOT touch. Deriving it produces a
misleading scope row, so auto-derivation drops it; the operator can still
declare it explicitly via --scope.
"""

import importlib.util
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_insert",
    os.path.join(BIN, "tusk-task-insert.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestIssueRepro:
    TEXT = (
        "tailwind.css points at src/app/globals.css "
        "(does not exist; real path is app/globals.css)"
    )

    def test_counterfactual_path_dropped(self):
        candidates = mod._auto_scope_candidates(self.TEXT)
        assert "src/app/globals.css" not in candidates

    def test_real_path_after_semicolon_kept(self):
        candidates = mod._auto_scope_candidates(self.TEXT)
        assert "app/globals.css" in candidates


class TestTrailingNegationWindow:
    @pytest.mark.parametrize(
        "text",
        [
            "skills/foo/SKILL.md (does not exist)",
            "skills/foo/SKILL.md (deleted)",
            "skills/foo/SKILL.md (removed in TASK-12)",
            "skills/foo/SKILL.md (no longer exists)",
            "skills/foo/SKILL.md doesn't exist anymore",
            "skills/foo/SKILL.md was deleted last week",
            "skills/foo/SKILL.md is nonexistent",
        ],
    )
    def test_trailing_negation_drops_path(self, text):
        assert "skills/foo/SKILL.md" not in mod._auto_scope_candidates(text)

    def test_sentence_boundary_blocks_trailing_window(self):
        # The negation belongs to the next sentence, not the path.
        text = "Edit skills/foo/SKILL.md. The old marker does not exist."
        assert "skills/foo/SKILL.md" in mod._auto_scope_candidates(text)


class TestLeadingNegationWindow:
    def test_not_within_clause_drops_path(self):
        text = "do not edit bin/tusk-merge.py for this change"
        assert "bin/tusk-merge.py" not in mod._auto_scope_candidates(text)

    def test_never_within_clause_drops_path(self):
        text = "the loader never reads conf/settings.yaml at runtime"
        assert "conf/settings.yaml" not in mod._auto_scope_candidates(text)

    def test_clause_boundary_resets_leading_window(self):
        text = "do not edit bin/tusk-merge.py; the fix belongs in bin/tusk-task-insert.py"
        candidates = mod._auto_scope_candidates(text)
        assert "bin/tusk-merge.py" not in candidates
        assert "bin/tusk-task-insert.py" in candidates


class TestPositiveMentionsUnchanged:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Fix bin/tusk-task-insert.py extraction", "bin/tusk-task-insert.py"),
            ("Update docs/DOMAIN.md and docs/HOOKS.md", "docs/DOMAIN.md"),
            ("Note: src/util.py exists already", "src/util.py"),
        ],
    )
    def test_non_negated_paths_keep_deriving(self, text, expected):
        assert expected in mod._auto_scope_candidates(text)

    def test_mixed_mentions_keep_path_when_any_positive(self):
        # One negated mention plus one positive mention — keep the path.
        text = (
            "bin/tusk-merge.py (deleted) was restored; "
            "edit bin/tusk-merge.py to add the guard"
        )
        assert "bin/tusk-merge.py" in mod._auto_scope_candidates(text)


class TestNegatedPathMentionsHelper:
    def test_unmentioned_candidate_untouched(self):
        # Inferred candidates without a literal mention never drop.
        negated = mod._negated_path_mentions(
            "regenerate the lockfile", ["apps/web/package-lock.json"]
        )
        assert negated == set()

    def test_basename_fallback_for_resolved_candidates(self):
        # A bare-basename mention resolved to a repo path is matched via its
        # basename token when the full path is absent from the text.
        negated = mod._negated_path_mentions(
            "settings.yaml (deleted) is gone", ["conf/settings.yaml"]
        )
        assert negated == {"conf/settings.yaml"}
