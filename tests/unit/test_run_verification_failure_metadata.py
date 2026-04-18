"""Unit tests for run_verification failure metadata (TASK-66).

On failure, the output must start with `exit_code=<N>, elapsed=<Xs>` so users
can distinguish a genuine non-zero exit from a subprocess timeout. Test-type
criteria also get a longer timeout (300s) than code-type (120s), since
subprocess.run(capture_output=True) slows pytest substantially.
"""

import importlib.util
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


def test_failure_output_prefixed_with_exit_code_and_elapsed():
    result = criteria_mod.run_verification("code", "exit 7")
    assert result["passed"] is False
    assert re.match(r"exit_code=7, elapsed=\d+\.\d+s\n", result["output"]), result["output"]


def test_success_output_has_no_metadata_header():
    result = criteria_mod.run_verification("code", "true")
    assert result["passed"] is True
    assert "exit_code=" not in result["output"]
    assert "elapsed=" not in result["output"]


def test_failure_header_survives_truncation():
    # Produce > 2000 chars of stdout, then exit non-zero. The exit_code/elapsed
    # header is prepended before truncation, so it must remain visible.
    spec = "printf 'x%.0s' {1..3000}; exit 2"
    result = criteria_mod.run_verification("code", spec)
    assert result["passed"] is False
    assert result["output"].startswith("exit_code=2, elapsed="), result["output"][:120]
    assert result["output"].endswith("... (truncated)")


def test_test_type_has_longer_timeout_than_code_type():
    assert criteria_mod._TEST_TIMEOUT_SECS >= 300
    assert criteria_mod._TEST_TIMEOUT_SECS > criteria_mod._CODE_TIMEOUT_SECS


def test_timeout_output_reports_timeout_marker(monkeypatch):
    # Force timeout without waiting 300s by monkeypatching the constant.
    monkeypatch.setattr(criteria_mod, "_CODE_TIMEOUT_SECS", 1)
    result = criteria_mod.run_verification("code", "sleep 5")
    assert result["passed"] is False
    assert result["output"].startswith("exit_code=timeout, elapsed="), result["output"]
    assert "Verification timed out (1s)" in result["output"]
