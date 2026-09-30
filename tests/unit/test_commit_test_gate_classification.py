"""Unit tests for the test-gate unavailable-vs-failed classification
(issue #1067).

``_test_command_unavailable`` used to match bare substrings ("not found",
"no such file or directory") anywhere in captured test output, so a genuinely
failing test whose output contained those phrases (e.g. a vitest assertion
about a 404/"Podcast not found" page) was misrouted to the linked-worktree
"test_command is unavailable" diagnostic — steering agents toward
--skip-verify while a real regression existed. Classification now requires
exit 126/127 or a line-anchored shell-execution-error signature, and
recognizable test-runner output always routes to the tests-failed path.
"""

import importlib.util
import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _result(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        ["test-cmd"], returncode, stdout=stdout, stderr=stderr
    )


class TestGenuineTestFailuresAreNotUnavailable:
    def test_vitest_assertion_about_not_found_page(self):
        """The issue #1067 incident shape: vitest summary present, assertion
        text contains 'not found', exit 1 — the suite ran; not unavailable."""
        mod = _load_module()
        stdout = (
            " FAIL  app/podcast/[id]/page.test.tsx > renders not found state\n"
            "AssertionError: expected 'Podcast not found' to be 'Podcast Not Found'\n"
            " Test Files  1 failed (1)\n"
            "      Tests  1 failed | 4 passed (5)\n"
        )
        assert mod._test_command_unavailable(_result(1, stdout=stdout)) is False

    def test_pytest_failure_mentioning_no_such_file(self):
        """A pytest test asserting on a missing-file error message must not
        be classified unavailable just because the phrase appears."""
        mod = _load_module()
        stdout = (
            "E  FileNotFoundError: [Errno 2] No such file or directory: 'x'\n"
            "=========== 1 failed, 12 passed in 3.21s ===========\n"
        )
        assert mod._test_command_unavailable(_result(1, stdout=stdout)) is False

    def test_jest_summary_with_not_found_in_stderr(self):
        mod = _load_module()
        stderr = (
            "  ● renders 404 page › shows not found\n"
            "Tests: 1 failed, 7 passed, 8 total\n"
        )
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is False

    def test_go_test_failure(self):
        mod = _load_module()
        stdout = "--- FAIL: TestLookup (0.00s)\nFAIL\texample.com/pkg\t0.01s\n"
        assert mod._test_command_unavailable(_result(1, stdout=stdout)) is False

    def test_plain_exit_1_without_signatures_is_not_unavailable(self):
        """Exit 1 with no runner output and no shell-error signature is an
        ordinary failure — bare 'not found' inside arbitrary output must not
        flip it to unavailable."""
        mod = _load_module()
        stderr = "Error: route handler said: resource not found in cache\n"
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is False


class TestEnvironmentalFailuresStayUnavailable:
    def test_exit_127_is_authoritative(self):
        mod = _load_module()
        assert mod._test_command_unavailable(_result(127)) is True

    def test_exit_126_is_authoritative(self):
        mod = _load_module()
        assert mod._test_command_unavailable(_result(126)) is True

    def test_exit_127_wins_even_with_runner_output(self):
        """npm test passes (summary printed) then a chained type-check tool
        is missing: the shell's 127 still means the command chain could not
        complete for environmental reasons."""
        mod = _load_module()
        stdout = " Test Files  3 passed (3)\n"
        stderr = "sh: tsc: command not found\n"
        assert (
            mod._test_command_unavailable(_result(127, stdout=stdout, stderr=stderr))
            is True
        )

    def test_bash_command_not_found_line(self):
        mod = _load_module()
        stderr = "bash: line 1: pnpm: command not found\n"
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is True

    def test_dash_not_found_line(self):
        mod = _load_module()
        stderr = "sh: 1: vitest: not found\n"
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is True

    def test_absolute_shell_path_no_such_file(self):
        mod = _load_module()
        stderr = "/bin/sh: .venv/bin/python3: No such file or directory\n"
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is True

    def test_env_no_such_file(self):
        mod = _load_module()
        stderr = "env: node: No such file or directory\n"
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is True

    def test_cd_into_missing_dir(self):
        """Wrapper-setup failure (cd into a dir absent from the worktree)."""
        mod = _load_module()
        stderr = "sh: line 0: cd: apps/web: No such file or directory\n"
        assert mod._test_command_unavailable(_result(1, stderr=stderr)) is True


def test_gate_evidence_rejects_skipped_only_hidden_test_runner():
    mod = _load_module()
    skipped = 'Test Files 1 skipped (1)\nTests 10 skipped (10)\n'
    validated, error = mod._test_gate_evidence(_result(0, stderr=skipped))
    assert validated is False
    assert 'zero tests executed' in error
    assert mod._test_gate_evidence(_result(7, stderr=skipped)) == (False, None)
    assert mod._test_gate_evidence(_result(0, stdout=None, stderr=None)) == (False, None)
    assert mod._test_gate_evidence(_result(0, stdout='Test Files 1 passed (1)\nTests 1 passed | 9 skipped (10)')) == (True, None)


def test_gate_handoff_clears_inherited_evidence_and_requires_validated_output(monkeypatch):
    mod = _load_module()
    for key in ('TUSK_COMMIT_GATE_COMMAND', 'TUSK_COMMIT_GATE_SHA',
                'TUSK_COMMIT_GATE_VALIDATED', 'TUSK_COMMIT_VERIFICATION_CACHE_SHA'):
        monkeypatch.setenv(key, 'stale')
    for validated, sha, command in ((False, 'newsha', 'npm test'), (True, None, 'npm test'), (True, 'newsha', None)):
        env = mod._criteria_verification_env(sha, command, validated)
        assert not any(key.startswith('TUSK_COMMIT_GATE_') for key in env)
        assert env.get('TUSK_COMMIT_VERIFICATION_CACHE_SHA') == sha
    env = mod._criteria_verification_env('newsha', 'npm test', True)
    assert env['TUSK_COMMIT_GATE_COMMAND'] == 'npm test'
    assert env['TUSK_COMMIT_GATE_SHA'] == 'newsha'
    assert env['TUSK_COMMIT_GATE_VALIDATED'] == '1'
