"""Unit tests for tusk-init-scan-todos.py false-positive filtering.

Covers the five false-positive patterns from GitHub Issue #445:
1. String literal matches (todo inside quoted text)
2. Identifier matches (TodoWrite, todoList)
3. Developer-tagged notes (TODO(inigo): ...)
4. Short / truncated summaries (< 10 chars or < 2 words)
5. Code fragment summaries ()., /types.js)

Also verifies that legitimate TODO comments are still detected.
"""

import importlib.util
import os
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load the module (hyphenated filename requires importlib)
_spec = importlib.util.spec_from_file_location(
    "tusk_init_scan_todos",
    os.path.join(REPO_ROOT, "bin", "tusk-init-scan-todos.py"),
)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


def _scan_lines(lines: list[str]) -> list[dict]:
    """Write lines to a temp file and scan it, returning the results."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_file.ts")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return scanner.scan(tmpdir)


# ── False positive: string literals ──────────────────────────────────


class TestStringLiterals:
    def test_todo_in_double_quoted_string(self):
        results = _scan_lines(['"use the todo list to track progress"'])
        assert results == []

    def test_todo_in_single_quoted_string(self):
        results = _scan_lines(["'use the todo list to track progress'"])
        assert results == []

    def test_todo_in_backtick_template_literal(self):
        results = _scan_lines(["`use the todo list to track progress`"])
        assert results == []


# ── False positive: identifiers ──────────────────────────────────────


class TestIdentifiers:
    def test_TodoWrite_in_jsdoc(self):
        results = _scan_lines(
            ["/** TodoWrite updates the todo panel, not the transcript). */"]
        )
        assert results == []

    def test_todoList_variable(self):
        results = _scan_lines(["const todoList = getTodos()"])
        assert results == []

    def test_myTODOHandler_camelcase(self):
        results = _scan_lines(["function myTODOHandler() {}"])
        assert results == []


# ── False positive: developer name tags ──────────────────────────────


class TestDeveloperTags:
    def test_parenthesized_name_with_colon(self):
        results = _scan_lines(
            ["// TODO(inigo): Refactor once AST parsing lands"]
        )
        assert results == []

    def test_hyphenated_name_tag(self):
        results = _scan_lines(
            ["// TODO(xaa-ga): add lockfile before GA"]
        )
        assert results == []

    def test_dotted_name_tag(self):
        results = _scan_lines(
            ["# FIXME(j.doe): handle edge case properly"]
        )
        assert results == []


# ── False positive: short / truncated summaries ──────────────────────


class TestShortSummaries:
    def test_single_word_too_short(self):
        results = _scan_lines(["// TODO: fix"])
        assert results == []

    def test_closing_paren_dot(self):
        results = _scan_lines(["// TODO: )."])
        assert results == []

    def test_empty_after_colon(self):
        results = _scan_lines(["// TODO:"])
        assert results == []


# ── False positive: code fragments ───────────────────────────────────


class TestCodeFragments:
    def test_bare_path(self):
        results = _scan_lines(["// TODO: /types.js"])
        assert results == []

    def test_file_extension_only(self):
        results = _scan_lines(["// TODO: types.js"])
        assert results == []

    def test_pure_punctuation(self):
        results = _scan_lines(["// TODO: )."])
        assert results == []


# ── Valid TODOs still detected ───────────────────────────────────────


class TestValidTodos:
    def test_hash_comment_todo(self):
        results = _scan_lines(["# TODO: Add rate limiting to login endpoint"])
        assert len(results) == 1
        assert results[0]["text"] == "Add rate limiting to login endpoint"
        assert results[0]["keyword"] == "TODO"

    def test_slash_comment_fixme(self):
        results = _scan_lines(["// FIXME: Memory leak in connection pool"])
        assert len(results) == 1
        assert results[0]["keyword"] == "FIXME"
        assert results[0]["priority"] == "High"
        assert results[0]["task_type"] == "bug"

    def test_block_comment_todo(self):
        results = _scan_lines(["/* TODO: Replace with proper error handling */"])
        assert len(results) == 1
        assert results[0]["text"] == "Replace with proper error handling"

    def test_jsdoc_star_todo(self):
        results = _scan_lines([" * TODO: Add validation for negative numbers"])
        assert len(results) == 1
        assert results[0]["text"] == "Add validation for negative numbers"

    def test_hack_keyword(self):
        results = _scan_lines(["# HACK: Workaround for upstream bug in libfoo"])
        assert len(results) == 1
        assert results[0]["keyword"] == "HACK"
        assert results[0]["priority"] == "High"

    def test_xxx_keyword(self):
        results = _scan_lines(["# XXX: This needs a proper implementation"])
        assert len(results) == 1
        assert results[0]["keyword"] == "XXX"

    def test_indented_comment(self):
        results = _scan_lines(["  # TODO: Indented comment with spaces"])
        assert len(results) == 1

    def test_tab_indented_comment(self):
        results = _scan_lines(["\t# TODO: Indented comment with tab"])
        assert len(results) == 1

    def test_double_hash(self):
        results = _scan_lines(["## TODO: Double hash comment style is valid"])
        assert len(results) == 1


# ── HTML comment delimiter ──────────────────────────────────────────


class TestHtmlComments:
    def test_html_comment_todo(self):
        results = _scan_lines(["<!-- TODO: Replace placeholder with real content -->"])
        assert len(results) == 1
        assert results[0]["text"] == "Replace placeholder with real content"
        assert results[0]["keyword"] == "TODO"

    def test_html_comment_fixme(self):
        results = _scan_lines(["<!-- FIXME: Broken layout on mobile devices -->"])
        assert len(results) == 1
        assert results[0]["keyword"] == "FIXME"
        assert results[0]["priority"] == "High"

    def test_html_comment_hack(self):
        results = _scan_lines(["<!-- HACK: Workaround for Safari flexbox bug -->"])
        assert len(results) == 1
        assert results[0]["keyword"] == "HACK"

    def test_html_comment_indented(self):
        results = _scan_lines(["    <!-- TODO: Add aria labels to navigation -->"])
        assert len(results) == 1
        assert results[0]["text"] == "Add aria labels to navigation"

    def test_html_comment_no_closing_arrow(self):
        """HTML comment without closing --> on same line."""
        results = _scan_lines(["<!-- TODO: Multi-line comment starts here"])
        assert len(results) == 1
        assert results[0]["text"] == "Multi-line comment starts here"

    def test_html_comment_strips_closing_arrow(self):
        """Trailing --> should be stripped from the text."""
        results = _scan_lines(["<!-- TODO: Fix broken links -->"])
        assert results[0]["text"] == "Fix broken links"
