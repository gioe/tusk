"""Unit tests for the per-rule timing breadcrumb in tusk-lint.py (issue #952).

`tusk commit` runs `tusk lint` as a subprocess with a hard timeout; on
TimeoutExpired the subprocess is killed (SIGKILL) and its stdout is lost, so
the abort message historically could not name the slow rule. When
TUSK_LINT_TRACE_FILE is set, lint overwrites it with the name of each rule
*before* running it. Because rules run sequentially, the file always holds the
in-flight rule, so the commit side can name it after a kill.

This test simulates the kill by making a rule raise: the breadcrumb has
already been written before the rule body runs, so the file holds that rule's
name — exactly what a real SIGKILL would leave behind.
"""

import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint", os.path.join(REPO_ROOT, "bin", "tusk-lint.py")
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def test_breadcrumb_records_inflight_rule(tmp_path, monkeypatch):
    trace_file = tmp_path / "trace"
    monkeypatch.setenv("TUSK_LINT_TRACE_FILE", str(trace_file))

    def ok(root):
        return []

    def boom(root):
        raise RuntimeError("simulating a hung/slow rule killed mid-run")

    monkeypatch.setattr(
        lint,
        "RULES",
        [
            ("Rule A: fast", ok, False),
            ("Rule B: the slow one", boom, False),
            ("Rule C: never reached", ok, False),
        ],
    )
    monkeypatch.setattr("sys.argv", ["tusk-lint.py", str(tmp_path), "--quiet"])

    with pytest.raises(RuntimeError):
        lint.main()

    # The breadcrumb names the rule that was running when execution stopped.
    assert trace_file.read_text(encoding="utf-8").strip() == "Rule B: the slow one"


def test_breadcrumb_overwritten_to_latest_rule(tmp_path, monkeypatch):
    # Sequential overwrite: after a clean rule completes, the file advances to
    # the next rule's name, so the last value is always the in-flight rule.
    trace_file = tmp_path / "trace"
    monkeypatch.setenv("TUSK_LINT_TRACE_FILE", str(trace_file))

    def ok(root):
        return []

    monkeypatch.setattr(
        lint,
        "RULES",
        [("Rule A", ok, False), ("Rule Z: last", ok, False)],
    )
    monkeypatch.setattr("sys.argv", ["tusk-lint.py", str(tmp_path), "--quiet"])

    with pytest.raises(SystemExit) as exc:
        lint.main()
    assert exc.value.code == 0
    assert trace_file.read_text(encoding="utf-8").strip() == "Rule Z: last"


def test_no_trace_file_env_is_noop(tmp_path, monkeypatch, capsys):
    # Without the env var, lint must run normally and never touch a trace file.
    monkeypatch.delenv("TUSK_LINT_TRACE_FILE", raising=False)

    def ok(root):
        return []

    monkeypatch.setattr(lint, "RULES", [("Rule A", ok, False)])
    monkeypatch.setattr("sys.argv", ["tusk-lint.py", str(tmp_path), "--verbose"])

    with pytest.raises(SystemExit) as exc:
        lint.main()
    assert exc.value.code == 0
    # --verbose prints per-rule elapsed time.
    assert "ms)" in capsys.readouterr().out
