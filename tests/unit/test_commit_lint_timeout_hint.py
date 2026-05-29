"""Unit tests for the lint-timeout slow-rule hint helpers in tusk-commit.py
(issue #952). On a lint timeout, the commit abort message names the rule that
was in-flight when the timeout fired, read from the breadcrumb file lint wrote.
"""

import importlib.util
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_commit", os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
)
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


def test_make_trace_env_points_lint_at_file():
    trace_path, env = commit._make_lint_trace_env()
    try:
        assert trace_path is not None
        assert env["TUSK_LINT_TRACE_FILE"] == trace_path
        assert os.path.exists(trace_path)
    finally:
        commit._cleanup_lint_trace(trace_path)
    assert not os.path.exists(trace_path)


def test_read_inflight_rule_returns_recorded_name(tmp_path):
    trace = tmp_path / "trace"
    trace.write_text("Rule 6: Done with incomplete acceptance criteria\n", encoding="utf-8")
    assert (
        commit._read_inflight_lint_rule(str(trace))
        == "Rule 6: Done with incomplete acceptance criteria"
    )


def test_read_inflight_rule_handles_missing_and_empty(tmp_path):
    assert commit._read_inflight_lint_rule(None) is None
    assert commit._read_inflight_lint_rule(str(tmp_path / "nope")) is None
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    assert commit._read_inflight_lint_rule(str(empty)) is None


def test_hung_rule_line_names_the_rule(tmp_path):
    trace = tmp_path / "trace"
    trace.write_text("Rule 15: Big-bang commits\n", encoding="utf-8")
    line = commit._lint_timeout_hung_rule_line(str(trace))
    assert "Rule 15: Big-bang commits" in line
    assert "running when the timeout fired" in line


def test_hung_rule_line_generic_fallback_when_unknown(tmp_path):
    # No breadcrumb -> the original generic wording is preserved.
    line = commit._lint_timeout_hung_rule_line(None)
    assert "A lint rule appears to be hung." in line
