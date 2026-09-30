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


def test_skipped_only_vitest_is_not_success(monkeypatch):
    monkeypatch.setattr(criteria_mod, '_get_repo_root', lambda: None)
    monkeypatch.setattr(criteria_mod.subprocess, 'run', lambda *a, **kw: criteria_mod.subprocess.CompletedProcess(a, 0, stdout=' Test Files  1 skipped (1)\n      Tests  10 skipped (10)\n', stderr=''))
    result = criteria_mod.run_verification('test', 'npm test')
    assert result['passed'] is False
    assert 'zero tests executed' in result['output']


def test_empty_vitest_failure_inspects_stderr_before_truncation(monkeypatch):
    monkeypatch.setattr(criteria_mod, '_get_repo_root', lambda: None)
    monkeypatch.setattr(criteria_mod.subprocess, 'run', lambda *a, **kw: criteria_mod.subprocess.CompletedProcess(
        a, 0, stdout='noise\n' * 600,
        stderr='\x1b[2m Test Files \x1b[0m 1 skipped (1)\n\x1b[2m      Tests \x1b[0m 10 skipped (10)\n',
    ))
    result = criteria_mod.run_verification('test', 'npm test -- -t old-name')
    assert result['passed'] is False
    assert result['output'].startswith('exit_code=0, elapsed=')
    assert 'zero tests executed' in result['output']
    assert result['output'].endswith('... (truncated)')


def test_vitest_detector_checks_each_summary_and_todo():
    passing = ' Test Files 1 passed (1)\n Tests 1 passed | 9 skipped (10)\n'
    skipped = ' Test Files 1 skipped (1)\n Tests 9 skipped | 1 todo (10)\n'
    assert criteria_mod.zero_executed_test_error(passing) is None
    assert criteria_mod.zero_executed_test_error(passing + skipped)
    assert criteria_mod.zero_executed_test_error(skipped + passing)
    assert criteria_mod.zero_executed_test_error(' Test Files 0 passed (0)\n Tests 0 passed (0)\n')
    for unrelated in ('', '10 skipped', 'Tests 10 skipped (10)', 'Tests: 10 skipped, 10 total',
                      'Test Files custom log\nTests 1 unknown (1)',
                      'Test Files 1 skipped (1)\nTests 3 skipped (10)'):
        assert criteria_mod.zero_executed_test_error(unrelated) is None


def test_zero_test_guard_preserves_code_checks_and_nonzero_exit(monkeypatch):
    monkeypatch.setattr(criteria_mod, '_get_repo_root', lambda: None)
    skipped = 'Test Files 1 skipped (1)\nTests 10 skipped (10)\n'
    monkeypatch.setattr(criteria_mod.subprocess, 'run', lambda *a, **kw: criteria_mod.subprocess.CompletedProcess(a, 0, stdout=skipped, stderr=''))
    assert criteria_mod.run_verification('code', 'npm test')['passed'] is True
    monkeypatch.setattr(criteria_mod.subprocess, 'run', lambda *a, **kw: criteria_mod.subprocess.CompletedProcess(a, 7, stdout=skipped, stderr='original error'))
    failed = criteria_mod.run_verification('test', 'npm test')
    assert failed['passed'] is False
    assert failed['output'].startswith('exit_code=7, elapsed=')
    assert 'original error' in failed['output']
    assert 'zero tests executed' not in failed['output']


def test_explicit_paired_no_tests_summary_is_empty_execution():
    assert criteria_mod.zero_executed_test_error(' Test Files no tests\n Tests no tests\n')
    assert criteria_mod.zero_executed_test_error('Tests no tests\n') is None
    assert criteria_mod.zero_executed_test_error('There are no tests for this message') is None
