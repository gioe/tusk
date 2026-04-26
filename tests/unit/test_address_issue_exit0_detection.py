"""Regression tests for address-issue skill Step 4.1 exit-0 detection.

These tests guard against accidentally removing the validation logic that runs
the extracted test_spec before storing it. The skill is a Markdown file, so the
tests read the SKILL.md text and assert that the key instructions are present.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")


def _skill_text():
    with open(SKILL_PATH) as f:
        return f.read()


class TestAddressIssueStep41ExitDetection:
    def test_skill_validates_test_spec_by_running_it(self):
        """Step 4.1 must instruct running the spec via bash -c."""
        text = _skill_text()
        assert "bash -c" in text, (
            "Step 4.1 must instruct running the test_spec via 'bash -c' to validate it"
        )

    def test_skill_handles_exit_0_with_warning(self):
        """Step 4.1 must warn when the spec exits 0 (passes before any fix)."""
        text = _skill_text()
        # Look for the exit-0 branch instruction
        assert "Exit 0" in text or "exit 0" in text.lower(), (
            "Step 4.1 must handle the case where test_spec exits 0"
        )
        assert "Discard" in text or "discard" in text.lower(), (
            "Step 4.1 must offer to discard the spec when it exits 0"
        )

    def test_skill_applies_defer_bias_on_exit_0_discard(self):
        """Discarding an exit-0 spec must score test_present as 'no'."""
        text = _skill_text()
        # The exit-0 discard path must score test_present as "no"
        section_match = re.search(
            r'Exit 0.*?test_present.*?"no"',
            text,
            re.DOTALL | re.IGNORECASE,
        )
        assert section_match is not None, (
            "Step 4.1 must score test_present as 'no' when an exit-0 spec is discarded"
        )

    def test_skill_treats_command_error_as_null(self):
        """Step 4.1 must treat command errors (exit 126/127, syntax error) as test_spec=null."""
        text = _skill_text()
        assert "126" in text or "127" in text or "command not found" in text.lower(), (
            "Step 4.1 must detect command-not-found / syntax-error exit codes"
        )
        # The command-error path must score test_present as "no"
        section_match = re.search(
            r'(126|127|command.?not.?found|syntax error).*?test_present.*?"no"',
            text,
            re.DOTALL | re.IGNORECASE,
        )
        assert section_match is not None, (
            "Step 4.1 must score test_present as 'no' on command errors"
        )

    def test_skill_proceeds_normally_on_nonzero_exit(self):
        """Step 4.1 must proceed normally when the spec exits nonzero (expected failure)."""
        text = _skill_text()
        assert "Exit nonzero" in text or "exit nonzero" in text.lower(), (
            "Step 4.1 must explicitly state that an exit-nonzero spec is stored and used normally"
        )


class TestAddressIssueStep41WrapperHandling:
    """Step 4.1 fast-path must peel `bash -c '<body>'` / `sh -c '<body>'`
    wrappers before the PATH-resolution check (Issue #583, TASK-203).

    Without this peeling, every issue whose `## Failing Test` is wrapped in
    `bash -c '...'` (a recurring pattern in tusk's own issue templates — any
    regression spec that chains `tusk init && tusk task-insert ...` ends up
    wrapped this way) flows into the sandbox, which then runs the wrapper,
    which then invokes off-PATH project tools that exit 127. The skill
    misreads `bash: tusk: command not found` as a command error per the
    documented rule, sets test_spec=null, and scores test_present='no' —
    flipping addressable bugs into Defer purely because the sandbox cannot
    reach the project tools the spec uses.
    """

    def test_skill_documents_wrapper_detection(self):
        """Step 4.1 fast-path must call out the `bash -c` / `sh -c` wrapper case."""
        text = _skill_text()
        assert re.search(r"`bash -c\b", text), (
            "Step 4.1 must mention `bash -c` wrapper handling"
        )
        assert re.search(r"`sh -c\b", text), (
            "Step 4.1 must mention `sh -c` wrapper handling"
        )

    def test_skill_inspects_wrapper_body_first_token(self):
        """Wrapper-detected specs must check the body's first token, not bash/sh."""
        text = _skill_text()
        assert re.search(
            r"wrapper body|body'?s first token|inner.{0,20}token|peel",
            text,
            re.IGNORECASE,
        ), (
            "Step 4.1 must instruct inspecting the wrapper body's first token "
            "(not the outer bash/sh) for the PATH-resolution check"
        )

    def test_skill_wrapper_branch_uses_fast_path_skip(self):
        """A `bash -c '<tusk-using>'` spec must take the same fast-path skip
        (test_spec=null, test_present='no') as a bare off-PATH spec — never
        falling through to the sandbox stderr-parse path."""
        text = _skill_text()
        # The fast-path skip block must mention wrapper detection alongside
        # the existing skip semantics (test_spec=null, test_present="no").
        section_match = re.search(
            r'(bash -c|sh -c|wrapper).*?test_spec\s*=\s*null.*?test_present\s*=\s*"?no"?',
            text,
            re.DOTALL | re.IGNORECASE,
        )
        assert section_match is not None, (
            "Step 4.1 must score test_present='no' via the fast-path when the "
            "wrapper body's first token is off the sandbox PATH (same skip "
            "semantics as a bare off-PATH spec — never via the sandbox "
            "stderr-parse path)"
        )

    def test_skill_drops_bash_sh_from_on_path_examples(self):
        """The 'effective token DOES resolve' fall-through example must not
        treat bare `bash`/`sh` as a sandbox-fall-through case anymore — that
        was the original bug. The wrapper-detection branch handles them now."""
        text = _skill_text()
        # Find the fall-through paragraph (the one that lists on-PATH examples
        # for falling through to sub-item b).
        fall_through_match = re.search(
            r"effective token DOES resolve.*?(\n\n|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        assert fall_through_match is not None, (
            "Step 4.1 must contain the 'effective token DOES resolve' fall-through paragraph"
        )
        fall_through_text = fall_through_match.group(0)
        # The example list must not present bare `bash` or `sh` as fall-through
        # cases — they are now handled by the wrapper-detection branch above.
        assert not re.search(r"e\.g\.[^)]*`bash`[,)]", fall_through_text), (
            "Fall-through examples must not list bare `bash` — wrapper detection "
            "now handles `bash -c` before the fall-through is reached"
        )
        assert not re.search(r"e\.g\.[^)]*`sh`[,)]", fall_through_text), (
            "Fall-through examples must not list bare `sh` — wrapper detection "
            "now handles `sh -c` before the fall-through is reached"
        )
