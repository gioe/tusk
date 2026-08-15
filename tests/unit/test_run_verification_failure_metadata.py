"""Unit tests for run_verification failure metadata (TASK-66).

On failure, the output must start with `exit_code=<N>, elapsed=<Xs>` so users
can distinguish a genuine non-zero exit from a subprocess timeout. Test-type
criteria honor the project test timeout with a 300s fallback; code-type
criteria retain their fixed 120s timeout.
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


def _captured_timeout(monkeypatch, criterion_type, config=None):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return criteria_mod.subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(criteria_mod, "_get_repo_root", lambda: None)
    monkeypatch.setattr(criteria_mod.subprocess, "run", fake_run)
    result = criteria_mod.run_verification(criterion_type, "true", config=config)
    assert result["passed"] is True
    return calls[0]["timeout"]


def test_test_type_honors_configured_timeout(monkeypatch):
    monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
    assert _captured_timeout(
        monkeypatch, "test", {"test_command_timeout_sec": 600}
    ) == 600


def test_test_type_missing_or_invalid_config_uses_legacy_default(monkeypatch):
    monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
    for config in (None, {}, {"test_command_timeout_sec": 0},
                   {"test_command_timeout_sec": True},
                   {"test_command_timeout_sec": "invalid"}):
        assert criteria_mod._test_verification_timeout(config) == 300


def test_test_type_environment_override_precedes_config(monkeypatch):
    monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "720")
    assert criteria_mod._test_verification_timeout(
        {"test_command_timeout_sec": 600}
    ) == 720


def test_invalid_environment_override_falls_through_to_config(monkeypatch):
    monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "invalid")
    assert criteria_mod._test_verification_timeout(
        {"test_command_timeout_sec": 600}
    ) == 600


def test_code_type_keeps_fixed_timeout_when_test_timeout_is_configured(monkeypatch):
    monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "720")
    assert _captured_timeout(
        monkeypatch, "code", {"test_command_timeout_sec": 600}
    ) == criteria_mod._CODE_TIMEOUT_SECS


def test_timeout_output_reports_timeout_marker(monkeypatch):
    # Force timeout without waiting 300s by monkeypatching the constant.
    monkeypatch.setattr(criteria_mod, "_CODE_TIMEOUT_SECS", 1)
    result = criteria_mod.run_verification("code", "sleep 5")
    assert result["passed"] is False
    assert result["output"].startswith("exit_code=timeout, elapsed="), result["output"]
    assert "Verification timed out (1s)" in result["output"]
