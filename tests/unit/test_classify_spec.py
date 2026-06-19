"""Tests for the `tusk address-issue classify-spec` helper.

The helper centralises five chunks of logic that previously lived as Step 4.1
prose in skills/address-issue/SKILL.md:

  1. Effective first-token resolution (peeling `bash -c '<body>'` /
     `sh -c '<body>'` wrappers).
  2. Issue #589 short-circuit for `/`-containing tokens (bypassing
     `command -v`'s cwd-relative resolution).
  3. `command -v` PATH check on the sandbox PATH (/usr/bin:/bin).
  4. Sandbox-result classification: malformed, environmental,
     interpreter-wrapper-bypass, pass-through.
  5. The recommended downstream action for the calling skill.

Two layers of coverage:

  * Pure-Python helpers — wrapper peel, on-PATH resolution, post-sandbox
    classification — exercised directly by importing the module.
  * CLI behaviour — stdin / --spec / --spec-file inputs, exit codes, and the
    pre-flight "sandbox required" exit-2 path — exercised via subprocess
    against the `tusk address-issue classify-spec` dispatcher.

The TASK-314 (Python `-m` form) and issue #659 (text-tool environmental
exit 1/2) signatures are exercised explicitly: they are the two regression
fixtures the helper exists to close.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
SCRIPT = os.path.join(BIN, "tusk-address-issue.py")
TUSK = os.path.join(BIN, "tusk")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_address_issue", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _classify(mod, exit_code, stderr, decision=None, effective_first_token=""):
    """Convenience wrapper around classify_post_sandbox for assertion-heavy tests."""
    return mod.classify_post_sandbox(
        exit_code, stderr, decision, effective_first_token
    )


# ─── Effective first-token resolution ───────────────────────────────────────


class TestResolveEffectiveFirstToken:
    """Step 4.1.a: bash/sh -c wrapper peel + plain-spec passthrough."""

    def test_plain_spec_returns_first_token(self, mod):
        assert mod.resolve_effective_first_token("pytest -q tests/") == "pytest"

    def test_plain_spec_with_path_first_token(self, mod):
        # /-containing first token must round-trip verbatim — issue #589 is
        # about the on-PATH check, not the token resolution itself.
        assert mod.resolve_effective_first_token("bin/tusk task-list") == "bin/tusk"

    def test_bash_dash_c_single_quotes_peeled(self, mod):
        spec = "bash -c 'tusk init && tusk task-insert foo bar'"
        assert mod.resolve_effective_first_token(spec) == "tusk"

    def test_bash_dash_c_double_quotes_peeled(self, mod):
        spec = 'bash -c "grep -n foo CLAUDE.md"'
        assert mod.resolve_effective_first_token(spec) == "grep"

    def test_sh_dash_c_peeled(self, mod):
        # `sh -c` is documented alongside `bash -c` in Step 4.1.a; the same
        # peel applies regardless of which POSIX shell is named.
        spec = "sh -c 'pytest -q'"
        assert mod.resolve_effective_first_token(spec) == "pytest"

    def test_bash_without_dash_c_not_peeled(self, mod):
        # `bash` without `-c` is not the documented wrapper pattern; preserve
        # `bash` as the first token so the on-PATH check fires against bash
        # itself (which IS on /usr/bin:/bin).
        spec = "bash some/script.sh"
        assert mod.resolve_effective_first_token(spec) == "bash"

    def test_comment_lines_skipped_for_first_token(self, mod):
        spec = "# explanatory comment\npytest -q"
        assert mod.resolve_effective_first_token(spec) == "pytest"

    def test_empty_spec_returns_empty(self, mod):
        assert mod.resolve_effective_first_token("") == ""


class TestIsOnSandboxPath:
    """Step 4.1.a: PATH resolution + issue #589 short-circuit."""

    def test_known_system_tool_resolves(self, mod):
        # `grep` is guaranteed on /usr/bin:/bin on every POSIX system.
        assert mod.is_on_sandbox_path("grep") is True

    def test_unknown_token_does_not_resolve(self, mod):
        assert mod.is_on_sandbox_path("definitely-not-a-real-tool") is False

    def test_slash_containing_token_short_circuits(self, mod):
        # Issue #589: `command -v` would resolve `bin/grep` against cwd if cwd
        # contained a `bin/grep`. The helper short-circuits to False without
        # consulting the FS so a project-relative path the sandbox tempdir
        # cannot reach is correctly classified as off-PATH.
        assert mod.is_on_sandbox_path("bin/grep") is False
        assert mod.is_on_sandbox_path("/usr/local/bin/grep") is False

    def test_empty_token_is_off_path(self, mod):
        assert mod.is_on_sandbox_path("") is False


# ─── Post-sandbox classification ────────────────────────────────────────────


class TestClassifyMalformed:
    """Step 4.1.c — malformed spec branch (command not found / syntax error)."""

    def test_bash_command_not_found(self, mod):
        # The missing token IS the spec's own effective first command, so the
        # spec is genuinely malformed (issue #1114 only reroutes a DOWNSTREAM
        # tool to environmental).
        result = _classify(
            mod, 127, "bash: definitely-fake: command not found",
            effective_first_token="definitely-fake",
        )
        assert result["action"] == "discard"
        assert result["test_present"] == "no"
        assert "malformed" in result["reason"]

    def test_syntax_error(self, mod):
        result = _classify(mod, 2, "bash: -c: line 1: syntax error near unexpected token `;'")
        assert result["action"] == "discard"
        assert result["test_present"] == "no"

    def test_syntax_error_stays_malformed_regardless_of_token(self, mod):
        # A syntax error is unambiguously malformed even when an unrelated
        # downstream token would otherwise look environmental.
        result = _classify(
            mod, 2,
            "bash: tusk: command not found\nbash: -c: line 2: syntax error near unexpected token `)'",
            effective_first_token="cp",
        )
        assert result["action"] == "discard"
        assert result["test_present"] == "no"
        assert "malformed" in result["reason"]

    def test_127_unrecognized_stderr_routes_to_malformed(self, mod):
        # 126/127 + stderr matching neither environmental NSFOD nor explicit
        # command-not-found is treated as malformed per Step 4.1.c.
        result = _classify(mod, 127, "bash: pytest: Permission denied")
        assert result["action"] == "discard"
        assert result["test_present"] == "no"

    def test_command_not_found_unparseable_token_stays_malformed(self, mod):
        # "command not found" present but no token recoverable from the line —
        # fall back to malformed, preserving the historic behaviour.
        result = _classify(mod, 1, "command not found")
        assert result["action"] == "discard"
        assert result["test_present"] == "no"


class TestClassifyCommandNotFoundEnvironmental:
    """Issue #1114 regression — a `command not found` naming a downstream
    project tool the sandbox stripped from PATH is environmental, not malformed.

    The Step 4.1 sandbox runs under `env -i PATH=/usr/bin:/bin`, which removes
    `tusk` and every project-installed binary. A reproducer that runs an on-PATH
    command first (e.g. `cp`) and then drives `tusk` therefore emits
    `bash: tusk: command not found`, but the spec itself is sound — the missing
    token is a project tool unreachable from the sandbox, the same epistemic
    situation as the Step 4.1.a fast-path skip.
    """

    def test_downstream_tool_off_path_is_environmental(self, mod):
        # The #1114 scenario: effective first token `cp` ran (on-PATH), the
        # downstream `tusk` is missing.
        result = _classify(
            mod, 1, "bash: tusk: command not found",
            effective_first_token="cp",
        )
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "environmental" in result["reason"]
        assert "tusk" in result["reason"]

    def test_line_number_prefixed_form_extracts_token(self, mod):
        result = _classify(
            mod, 1, "bash: line 2: pytest: command not found",
            effective_first_token="cp",
        )
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "pytest" in result["reason"]

    def test_zsh_token_after_phrase_extracts_token(self, mod):
        result = _classify(
            mod, 1, "zsh: command not found: tusk",
            effective_first_token="cp",
        )
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "tusk" in result["reason"]

    def test_token_equal_to_effective_first_token_is_malformed(self, mod):
        # When the missing token IS the spec's own first command, it is genuinely
        # malformed — not the downstream-tool case.
        result = _classify(
            mod, 127, "bash: tusk: command not found",
            effective_first_token="tusk",
        )
        assert result["action"] == "discard"
        assert result["test_present"] == "no"
        assert "malformed" in result["reason"]

    def test_extract_command_not_found_token(self, mod):
        f = mod._extract_command_not_found_token
        assert f("bash: tusk: command not found") == "tusk"
        assert f("bash: line 2: tusk: command not found") == "tusk"
        assert f("tusk: command not found") == "tusk"
        assert f("zsh: command not found: tusk") == "tusk"
        assert f("no command-not-found line here") is None
        assert f("") is None


class TestClassifyEnvironmental126127:
    """Step 4.1.c — environmental branch for 126/127 exit codes."""

    def test_exit_127_with_no_such_file(self, mod):
        result = _classify(mod, 127, "bash: bin/tusk: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "environmental" in result["reason"]
        assert "127" in result["reason"]

    def test_exit_127_with_empty_stderr(self, mod):
        result = _classify(mod, 127, "")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"

    def test_exit_126_permission_denied_with_nsfod(self, mod):
        # 126 = command found but not executable / file-not-found from a
        # nested invocation. Treat identically to 127 environmental.
        result = _classify(mod, 126, "bash: scripts/run.sh: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"


class TestClassifyTextToolEnvironmental:
    """Issue #659 regression — exit 1/2 from text utilities with NSFOD stderr.

    The historic environmental branch gated on exit 126/127 only and missed
    grep/awk/sed/find which exit 1 or 2 when given a missing input file (they
    handle it internally rather than letting exec fail). The helper closes
    this gap by inspecting the stderr signature regardless of exit code.
    """

    def test_grep_exit_2_missing_file(self, mod):
        # The exact reproducer from issue #659: a polarity-correct grep
        # against project-relative paths the sandbox tempdir cannot reach.
        result = _classify(mod, 2, "grep: CLAUDE.md: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "environmental" in result["reason"]
        assert "grep" in result["reason"]

    def test_grep_exit_1_no_match_does_not_mark_environmental(self, mod):
        # grep exits 1 when no lines match — that's the *expected* failure
        # signal for a polarity-correct spec, NOT an environmental signature.
        # No NSFOD in stderr, so it falls through to "real failure".
        result = _classify(mod, 1, "")
        assert result["action"] == "store"
        assert result["test_present"] == "yes"

    def test_awk_exit_2_missing_input(self, mod):
        result = _classify(mod, 2, "awk: can't open file CLAUDE.md\nawk: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"

    def test_sed_exit_2_missing_input(self, mod):
        result = _classify(mod, 2, "sed: foo.txt: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "sed" in result["reason"]

    def test_find_exit_1_unreachable_path(self, mod):
        result = _classify(mod, 1, "find: missing/dir: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"


class TestClassifyInterpreterWrapperBypass:
    """Step 4.1.c — interpreter-wrapper-bypass branch.

    Each language signature must route to action="null" / test_present="unverifiable"
    when the inner subprocess token is off PATH=/usr/bin:/bin.
    """

    def test_python_filenotfounderror(self, mod):
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"<string>\", line 1, in <module>\n"
            "FileNotFoundError: [Errno 2] No such file or directory: 'tusk'"
        )
        result = _classify(mod, 1, stderr)
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "python" in result["reason"]
        assert "tusk" in result["reason"]

    def test_python_dash_m_module_not_found(self, mod):
        # TASK-314 regression fixture: python3 -m <module> fails before any
        # subprocess is spawned when the module is unimportable in the sandbox
        # env -i environment. <token> is a module name, not an executable, so
        # the on-PATH check is skipped and the result is always unverifiable.
        result = _classify(mod, 1, "/usr/bin/python3: No module named pytest")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "python -m" in result["reason"]
        assert "pytest" in result["reason"]
        assert "module" in result["reason"]

    def test_node_spawn_enoent(self, mod):
        result = _classify(mod, 1, "Error: spawn tusk ENOENT")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "node" in result["reason"]
        assert "tusk" in result["reason"]

    def test_node_trailing_enoent(self, mod):
        # Some Node versions emit a bare trailing "<token> ENOENT" line.
        result = _classify(mod, 1, "tusk ENOENT")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "node" in result["reason"]

    def test_ruby_enoent(self, mod):
        result = _classify(mod, 1, "Errno::ENOENT: No such file or directory - tusk")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "ruby" in result["reason"]
        assert "tusk" in result["reason"]

    def test_perl_cant_exec(self, mod):
        result = _classify(mod, 1, "Can't exec \"tusk\": No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "perl" in result["reason"]
        assert "tusk" in result["reason"]

    def test_generic_nsfod_off_path(self, mod):
        # Generic last-resort: a bare `<token>: No such file or directory`
        # line where <token> has no path component and is off PATH.
        result = _classify(mod, 1, "fakebin: No such file or directory")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"

    def test_python_token_path_component_stripped(self, mod):
        # A FileNotFoundError naming `bin/tusk` must strip the path component
        # before checking on-PATH (basename -> `tusk` -> off /usr/bin:/bin).
        stderr = "FileNotFoundError: [Errno 2] No such file or directory: 'bin/tusk'"
        result = _classify(mod, 1, stderr)
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"

    def test_python_token_on_path_falls_through_to_real_failure(self, mod):
        # When the inner subprocess token IS on /usr/bin:/bin, the wrapper
        # didn't bypass anything — the system tool genuinely failed inside
        # the wrapper. Fall through to "real failure" / store.
        stderr = "FileNotFoundError: [Errno 2] No such file or directory: 'grep'"
        # We expect grep to be on /usr/bin:/bin on the test runner; if
        # it isn't, skip the assertion rather than fail spuriously.
        if mod.is_on_sandbox_path("grep"):
            result = _classify(mod, 1, stderr)
            assert result["action"] == "store"
            assert result["test_present"] == "yes"


class TestClassifyPassThrough:
    """Step 4.1.c pass-through — exit nonzero with no command-error signature."""

    def test_real_failure_no_signature(self, mod):
        result = _classify(mod, 1, "AssertionError: expected 5, got 4")
        assert result["action"] == "store"
        assert result["test_present"] == "yes"
        assert "exited 1" in result["reason"]

    def test_pytest_failure(self, mod):
        # A typical pytest failure: nonzero exit, no NSFOD, no command-error.
        stderr = "FAILED tests/test_foo.py::test_bar - assert 1 == 2"
        result = _classify(mod, 1, stderr)
        assert result["action"] == "store"
        assert result["test_present"] == "yes"


class TestClassifyExitZero:
    """Step 4.1.c — exit 0 branch (spec passes before any fix)."""

    def test_default_routes_to_discard(self, mod):
        result = _classify(mod, 0, "")
        assert result["action"] == "discard"
        assert result["test_present"] == "no"

    def test_explicit_discard(self, mod):
        result = _classify(mod, 0, "", decision="discard")
        assert result["action"] == "discard"
        assert result["test_present"] == "no"

    def test_keep_routes_to_unverifiable(self, mod):
        result = _classify(mod, 0, "", decision="keep")
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"


# ─── CLI behaviour ──────────────────────────────────────────────────────────


class TestCLI:
    """End-to-end CLI behaviour via the `tusk address-issue classify-spec`
    dispatcher. These tests guarantee the helper is reachable through the
    documented entry point and that exit codes match the contract."""

    def test_preflight_off_path_exits_zero(self):
        # An off-PATH effective first token is the Step 4.1.a fast-path skip:
        # the helper does not need a sandbox run to classify.
        out = subprocess.check_output(
            [TUSK, "address-issue", "classify-spec", "--spec", "pytest -q"],
            encoding="utf-8",
        )
        result = json.loads(out)
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert result["effective_first_token"] == "pytest"
        assert result["on_path"] is False

    def test_preflight_on_path_exits_two(self):
        # An on-PATH effective first token cannot be classified without
        # sandbox results — exit 2 with a "sandbox required" stderr message.
        proc = subprocess.run(
            [TUSK, "address-issue", "classify-spec", "--spec", "grep -n foo /tmp/x"],
            capture_output=True,
            encoding="utf-8",
        )
        assert proc.returncode == 2
        assert "Sandbox required" in proc.stderr
        assert "grep" in proc.stderr

    def test_preflight_slash_token_short_circuits(self):
        # Issue #589: even though `bin/tusk` could resolve against cwd, the
        # `/`-containing short-circuit must classify it as off-PATH.
        out = subprocess.check_output(
            [TUSK, "address-issue", "classify-spec", "--spec", "bin/tusk task-list"],
            encoding="utf-8",
        )
        result = json.loads(out)
        assert result["effective_first_token"] == "bin/tusk"
        assert result["on_path"] is False
        assert result["action"] == "null"

    def test_post_sandbox_environmental_grep_issue_659(self):
        # Issue #659 reproducer end-to-end through the dispatcher.
        out = subprocess.check_output(
            [
                TUSK, "address-issue", "classify-spec",
                "--spec", "grep -lE 'pattern' CLAUDE.md docs/HOOKS.md",
                "--sandbox-exit", "2",
                "--sandbox-stderr", "grep: CLAUDE.md: No such file or directory",
            ],
            encoding="utf-8",
        )
        result = json.loads(out)
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "environmental" in result["reason"]
        assert "grep" in result["reason"]

    def test_post_sandbox_command_not_found_downstream_tool_issue_1114(self):
        # Issue #1114 reproducer end-to-end through the dispatcher: a throwaway-DB
        # spec whose effective first token `cp` is on-PATH but whose downstream
        # `tusk` is stripped by the sandbox must classify as environmental
        # (unverifiable), not malformed.
        out = subprocess.check_output(
            [
                TUSK, "address-issue", "classify-spec",
                "--spec",
                "cp /tmp/live.db /tmp/x.db && tusk criteria finish-deferred --reason chain 1",
                "--sandbox-exit", "1",
                "--sandbox-stderr", "bash: tusk: command not found",
            ],
            encoding="utf-8",
        )
        result = json.loads(out)
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert result["effective_first_token"] == "cp"
        assert "environmental" in result["reason"]
        assert "tusk" in result["reason"]

    def test_post_sandbox_python_dash_m_task_314(self, tmp_path):
        # TASK-314 reproducer end-to-end: python3 -m pytest exits 1 with
        # `<python3 path>: No module named pytest` and must classify as
        # interpreter-wrapper-bypass / unverifiable.
        stderr_file = tmp_path / "stderr.txt"
        stderr_file.write_text("/usr/bin/python3: No module named pytest\n")
        out = subprocess.check_output(
            [
                TUSK, "address-issue", "classify-spec",
                "--spec", "python3 -m pytest tests/unit/",
                "--sandbox-exit", "1",
                "--sandbox-stderr-file", str(stderr_file),
            ],
            encoding="utf-8",
        )
        result = json.loads(out)
        assert result["action"] == "null"
        assert result["test_present"] == "unverifiable"
        assert "python -m" in result["reason"]
        assert "pytest" in result["reason"]

    def test_stdin_is_default_spec_source(self):
        proc = subprocess.run(
            [TUSK, "address-issue", "classify-spec"],
            input="pytest -q\n",
            capture_output=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["effective_first_token"] == "pytest"

    def test_spec_file_dash_reads_stdin(self):
        proc = subprocess.run(
            [TUSK, "address-issue", "classify-spec", "--spec-file", "-"],
            input="pytest -q\n",
            capture_output=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["effective_first_token"] == "pytest"

    def test_empty_spec_exits_two(self):
        proc = subprocess.run(
            [TUSK, "address-issue", "classify-spec", "--spec", ""],
            capture_output=True,
            encoding="utf-8",
        )
        assert proc.returncode == 2

    def test_help_does_not_error(self):
        proc = subprocess.run(
            [TUSK, "address-issue", "--help"],
            capture_output=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0
        assert "classify-spec" in proc.stdout

    def test_unknown_subcommand_exits_two(self):
        proc = subprocess.run(
            [TUSK, "address-issue", "not-a-real-subcommand"],
            capture_output=True,
            encoding="utf-8",
        )
        assert proc.returncode == 2
        assert "Unknown subcommand" in proc.stderr


# ─── SKILL.md collapse guard ────────────────────────────────────────────────


class TestSkillMdCollapse:
    """Guard that Step 4.1 in SKILL.md actually delegates to the helper.

    These assertions don't pin the exact prose — they only require the helper
    name to appear in Step 4.1's window of the file. If a future refactor
    inlines the logic again, this test breaks and forces a re-think.
    """

    def test_skill_md_references_helper(self):
        skill_md = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")
        with open(skill_md, encoding="utf-8") as f:
            text = f.read()
        # The helper is referenced by name in Step 4.1's collapsed prose.
        assert "tusk address-issue classify-spec" in text, (
            "Step 4.1 must delegate to the classify-spec helper instead of "
            "duplicating the per-branch decision tree as inline prose."
        )
