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

    def test_unrecognized_path_exits_0_with_empty_output(self):
        conn = make_db((1, "some convention", "skill"))
        rc, out, err = capture("an/unrecognized/path.xyz", conn)
        assert rc == 0
        assert out == ""
        assert err == ""

    def test_no_matching_conventions_exits_0_with_empty_output(self):
        conn = make_db()  # empty DB
        rc, out, err = capture("skills/foo/SKILL.md", conn)
        assert rc == 0
        assert out == ""

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
