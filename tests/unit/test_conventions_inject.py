"""Unit tests for tusk-conventions.py cmd_inject and derive_topics.

Uses an in-memory SQLite DB — no filesystem or subprocess required.
"""

import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_conventions",
    os.path.join(REPO_ROOT, "bin", "tusk-conventions.py"),
)
conventions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conventions)

derive_topics = conventions.derive_topics
cmd_inject = conventions.cmd_inject


def make_db(*rows: tuple[int, str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE conventions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source_skill TEXT,
            topics TEXT,
            violation_count INTEGER DEFAULT 0
        )"""
    )
    for cid, text, topics in rows:
        conn.execute(
            "INSERT INTO conventions (id, text, topics) VALUES (?, ?, ?)",
            (cid, text, topics),
        )
    conn.commit()
    return conn


class _NonClosingConn:
    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def capture(path: str, conn: sqlite3.Connection):
    args = SimpleNamespace(path=path)
    out, err = io.StringIO(), io.StringIO()
    wrapper = _NonClosingConn(conn)
    orig = conventions.get_connection
    conventions.get_connection = lambda _: wrapper
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_inject(args, ":memory:", {})
    finally:
        conventions.get_connection = orig
    return rc, out.getvalue(), err.getvalue()


# ── derive_topics ──────────────────────────────────────────────────────

class TestDeriveTopics:
    def test_skill_md_returns_skill_and_docs(self):
        topics = derive_topics("skills/foo/SKILL.md")
        assert "skill" in topics
        assert "docs" in topics

    def test_tusk_py_returns_cli_and_python(self):
        topics = derive_topics("bin/tusk-foo.py")
        assert "cli" in topics
        assert "python" in topics

    def test_test_py_returns_testing_and_python(self):
        topics = derive_topics("tests/unit/test_bar.py")
        assert "testing" in topics
        assert "python" in topics

    def test_unrecognized_path_returns_empty(self):
        assert derive_topics("an/unrecognized/path.xyz") == []

    def test_plain_md_returns_docs(self):
        topics = derive_topics("docs/README.md")
        assert "docs" in topics
        assert "skill" not in topics

    def test_plain_py_outside_bin_returns_python(self):
        topics = derive_topics("lib/helpers.py")
        assert "python" in topics
        assert "cli" not in topics

    def test_non_tusk_py_in_bin_returns_python_not_cli(self):
        topics = derive_topics("bin/something.py")
        assert "python" in topics
        assert "cli" not in topics

    def test_skills_path_without_md_returns_skill_only(self):
        topics = derive_topics("skills/foo/main.py")
        assert "skill" in topics
        assert "python" in topics
        assert "docs" not in topics

    def test_absolute_path_skills(self):
        topics = derive_topics("/repo/skills/bar/SKILL.md")
        assert "skill" in topics
        assert "docs" in topics

    def test_test_filename_without_tests_dir(self):
        topics = derive_topics("src/test_utils.py")
        assert "testing" in topics
        assert "python" in topics

    def test_test_tsx_returns_vitest_testing_mocking(self):
        """Issue #1053: a *.test.tsx file derives the vitest test topics so
        topic-tagged conventions surface for it.
        """
        topics = derive_topics("apps/web/ui/pages/entity/podcast/index.test.tsx")
        assert "vitest" in topics
        assert "testing" in topics
        assert "mocking" in topics

    def test_spec_ts_returns_vitest_topics(self):
        topics = derive_topics("src/util.spec.ts")
        assert "vitest" in topics
        assert "testing" in topics
        assert "mocking" in topics

    def test_test_js_and_mts_siblings_match(self):
        for path in ("foo.test.js", "bar.test.jsx", "baz.spec.mts", "qux.test.cjs"):
            topics = derive_topics(path)
            assert "vitest" in topics, path
            assert "testing" in topics, path

    def test_plain_tsx_is_not_a_test_file(self):
        """A non-test .tsx component must NOT pick up the vitest topics."""
        topics = derive_topics("apps/web/ui/components/Button.tsx")
        assert topics == []

    def test_result_is_sorted(self):
        topics = derive_topics("skills/foo/SKILL.md")
        assert topics == sorted(topics)


# ── cmd_inject ─────────────────────────────────────────────────────────

class TestCmdInject:
    def test_skill_path_returns_skill_convention(self):
        conn = make_db((1, "skill convention text", "skill"), (2, "unrelated", "other"))
        rc, out, _ = capture("skills/foo/SKILL.md", conn)
        assert rc == 0
        assert "skill convention text" in out

    def test_tusk_py_returns_cli_convention(self):
        conn = make_db((1, "cli rule", "cli,python"), (2, "unrelated", "other"))
        rc, out, _ = capture("bin/tusk-foo.py", conn)
        assert rc == 0
        assert "cli rule" in out

    def test_test_py_returns_testing_convention(self):
        conn = make_db((1, "testing rule", "testing"), (2, "unrelated", "other"))
        rc, out, _ = capture("tests/unit/test_bar.py", conn)
        assert rc == 0
        assert "testing rule" in out

    def test_test_tsx_surfaces_vitest_convention(self):
        """Issue #1053 end-to-end: a *.test.tsx path injects a vitest-tagged
        convention that was previously lost to the silent empty result.
        """
        conn = make_db(
            (1, "happy-dom docblock requirement", "vitest"),
            (2, "unrelated python rule", "python"),
        )
        rc, out, _ = capture("apps/web/ui/pages/entity/podcast/index.test.tsx", conn)
        assert rc == 0
        assert "happy-dom docblock requirement" in out
        assert "unrelated python rule" not in out

    def test_unrecognized_path_prints_no_topics_diagnostic(self):
        """Issue #1053: a path that derives no topics must print a one-line
        diagnostic on stdout (naming the path + total count + the search
        pointer) instead of exiting silently with empty output.
        """
        conn = make_db((1, "some convention", "skill"))
        rc, out, err = capture("an/unrecognized/path.xyz", conn)
        assert rc == 0
        assert out != ""
        assert "an/unrecognized/path.xyz" in out
        assert "no topics could be derived" in out
        assert "1 convention(s) exist" in out
        assert "tusk conventions search" in out

    def test_no_matching_conventions_prints_no_match_diagnostic(self):
        """Issue #1053: topics derived but no convention matched them must
        print a diagnostic on stdout, not exit silently.
        """
        conn = make_db()  # empty DB
        rc, out, err = capture("skills/foo/SKILL.md", conn)
        assert rc == 0
        assert out != ""
        assert "skills/foo/SKILL.md" in out
        assert "matched no convention tags" in out
        # empty DB → the "no conventions are recorded" tail, not a count
        assert "no conventions are recorded" in out

    def test_topics_derived_but_no_match_reports_count_and_topics(self):
        """The diagnostic for the topics-derived-but-unmatched path names the
        derived topics and the total convention count (issue #1053).
        """
        # 'other'-tagged row exists, but skills/foo/SKILL.md derives skill/docs
        conn = make_db((1, "unrelated", "other"), (2, "also unrelated", "misc"))
        rc, out, _ = capture("skills/foo/SKILL.md", conn)
        assert rc == 0
        assert "docs" in out and "skill" in out  # derived topics listed
        assert "2 convention(s) exist" in out
        assert "tusk conventions search" in out

    def test_deduplication_across_topics(self):
        # A convention tagged 'skill,docs' should appear only once
        # when path matches both topics (e.g. skills/foo/SKILL.md)
        conn = make_db((1, "shared convention", "skill,docs"))
        rc, out, _ = capture("skills/foo/SKILL.md", conn)
        assert rc == 0
        assert out.count("shared convention") == 1

    def test_output_includes_total_line(self):
        conn = make_db((1, "a convention", "skill"))
        rc, out, _ = capture("skills/foo/SKILL.md", conn)
        assert rc == 0
        assert "Total:" in out

    def test_text_mentioning_topic_but_not_tagged_is_not_injected(self):
        """Issue #859 regression: cmd_inject must NOT pull in a convention
        whose `text` mentions a topic keyword in prose when the convention's
        `topics` column does not include that tag. The previous
        `text LIKE ? OR topics LIKE ?` filter over-matched 2-3x on every
        Edit/Write; the strict comma-anchored topics filter prevents that.
        """
        conn = make_db(
            (1, "Always pass encoding='utf-8' when running in Python scripts.", "cli"),
            (2, "Some testing convention with the word python in its body.", "testing"),
        )
        # bin/foo.py derives topics = ['python']. With the strict filter,
        # only conventions tagged 'python' should match — neither of the
        # above has 'python' in its `topics` column.
        rc, out, _ = capture("lib/helpers.py", conn)
        assert rc == 0
        assert "Total: 0" not in out  # no Total line on zero rows
        assert "Always pass encoding" not in out
        assert "Some testing convention" not in out

    def test_topic_tag_match_still_injects(self):
        """Companion regression: a convention whose `topics` column includes
        the derived tag must still be injected after the filter tightens.
        """
        conn = make_db(
            (1, "Real python convention", "python,cli"),
            (2, "Body mentions python only in prose", "docs"),
        )
        rc, out, _ = capture("lib/helpers.py", conn)
        assert rc == 0
        assert "Real python convention" in out
        assert "Body mentions python only in prose" not in out

    def test_prefix_overlap_does_not_false_match(self):
        """The comma-anchored filter must not let 'test' match a convention
        tagged 'testing' (or vice versa) — substring collisions inside the
        topic list are exactly what the strict filter exists to prevent.
        """
        conn = make_db(
            (1, "tagged with testing not test", "testing,pytest"),
        )
        # Use a path whose derived topic set is exactly ['python']; nothing
        # there should match 'testing'. The path triggers no 'testing' topic.
        rc, out, _ = capture("lib/helpers.py", conn)
        assert rc == 0
        assert "tagged with testing not test" not in out
