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
        """Discarding an exit-0 spec must apply Factor 0 Defer bias."""
        text = _skill_text()
        # The exit-0 discard path and Factor 0 / Defer bias must be co-located
        section_match = re.search(
            r"Exit 0.*?Factor 0 Defer bias",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        assert section_match is not None, (
            "Step 4.1 must apply Factor 0 Defer bias when an exit-0 spec is discarded"
        )

    def test_skill_treats_command_error_as_null(self):
        """Step 4.1 must treat command errors (exit 126/127, syntax error) as test_spec=null."""
        text = _skill_text()
        assert "126" in text or "127" in text or "command not found" in text.lower(), (
            "Step 4.1 must detect command-not-found / syntax-error exit codes"
        )
        # The command-error path must also apply Defer bias
        section_match = re.search(
            r"(126|127|command.?not.?found|syntax error).*?Factor 0 Defer bias",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        assert section_match is not None, (
            "Step 4.1 must apply Factor 0 Defer bias on command errors"
        )

    def test_skill_proceeds_normally_on_nonzero_exit(self):
        """Step 4.1 must proceed normally when the spec exits nonzero (expected failure)."""
        text = _skill_text()
        assert "Exit nonzero" in text or "exit nonzero" in text.lower(), (
            "Step 4.1 must explicitly state that an exit-nonzero spec is stored and used normally"
        )
