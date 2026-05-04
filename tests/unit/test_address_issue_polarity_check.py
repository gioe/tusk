"""Regression tests for address-issue skill Step 7.5 polarity check (issue #642).

These tests guard against accidentally removing the post-implementation polarity
re-run that catches inverted-polarity failing-test specs (e.g. `test -z "$(...)"`,
leading `!`) which exit nonzero on broken AND fixed code. The skill is a Markdown
file, so the tests read SKILL.md / the issue template text and assert that the
key instructions are present.

Original incident: TASK-287 / issue #591, criterion #1291 was deferred via
skip-verify with a polarity-inversion rationale; coverage was replaced by a
pytest regression. This skill change adds the missing detection step.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")
TEMPLATE_PATH = os.path.join(
    REPO_ROOT, ".github", "ISSUE_TEMPLATE", "tusk-instance-feedback.yml"
)


def _skill_text():
    with open(SKILL_PATH) as f:
        return f.read()


def _template_text():
    with open(TEMPLATE_PATH) as f:
        return f.read()


class TestStep75PolarityCheckPresent:
    def test_skill_has_step_75_section(self):
        """Step 7.5 must be a top-level section between Step 7 and Steps 8–10."""
        text = _skill_text()
        assert re.search(
            r"##\s+Step\s+7\.5\s*:\s*Polarity Check",
            text,
            re.IGNORECASE,
        ), "Step 7.5 section heading must exist"

    def test_step_75_appears_between_step_7_and_step_8(self):
        """Step 7.5 must be ordered after Step 7 and before the Step 8 finalize block."""
        text = _skill_text()
        s7 = text.find("## Step 7:")
        s75 = re.search(r"##\s+Step\s+7\.5", text)
        s8 = text.find("## Steps 8")
        assert s7 != -1 and s75 is not None and s8 != -1, (
            "Step 7, 7.5, and 8 anchors must all be present"
        )
        assert s7 < s75.start() < s8, (
            "Step 7.5 must fall between Step 7 and Steps 8–10 in document order"
        )

    def test_step_75_fetches_test_type_criteria(self):
        """Step 7.5 must query for open test-type criteria with verification specs."""
        text = _skill_text()
        # The fetch query must filter on criterion_type='test', open, and not deferred.
        assert re.search(
            r"criterion_type\s*=\s*'test'", text
        ), "Step 7.5 must filter for criterion_type='test'"
        assert re.search(r"is_completed\s*=\s*0", text), (
            "Step 7.5 must filter for open (is_completed=0) criteria"
        )
        assert re.search(r"is_deferred\s*=\s*0", text), (
            "Step 7.5 must filter out already-deferred criteria"
        )

    def test_step_75_runs_criteria_done_per_row(self):
        """Step 7.5 must instruct running `tusk criteria done <cid>` per row."""
        text = _skill_text()
        # Find the Step 7.5 section content
        match = re.search(
            r"##\s+Step\s+7\.5.*?(?=\n##\s+Steps?\s+8|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None, "Could not isolate Step 7.5 section"
        section = match.group(0)
        assert re.search(r"tusk criteria done\s+<cid>", section), (
            "Step 7.5 must invoke `tusk criteria done <cid>` to re-run the spec post-fix"
        )


class TestStep75PolarityMismatchOptions:
    def test_skill_offers_invert_option(self):
        """Step 7.5 must offer to re-run the spec wrapped in `! ( ... )`."""
        text = _skill_text()
        match = re.search(
            r"##\s+Step\s+7\.5.*?(?=\n##\s+Steps?\s+8|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None
        section = match.group(0)
        assert re.search(r"\binvert\b", section, re.IGNORECASE), (
            "Step 7.5 must offer an 'invert' option"
        )
        # The wrapped form `! ( <spec> )` must be shown as the inversion mechanic.
        assert re.search(r"!\s*\(\s*<verification_spec>\s*\)", section), (
            "Step 7.5 must show the `! ( <verification_spec> )` wrap as the invert mechanic"
        )

    def test_skill_offers_skip_option(self):
        """Step 7.5 must offer to defer the criterion via `tusk criteria skip`."""
        text = _skill_text()
        match = re.search(
            r"##\s+Step\s+7\.5.*?(?=\n##\s+Steps?\s+8|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None
        section = match.group(0)
        assert re.search(r"tusk criteria skip\s+<cid>", section), (
            "Step 7.5 must invoke `tusk criteria skip <cid>` for the skip option"
        )
        assert re.search(r"polarity mismatch", section, re.IGNORECASE), (
            "The skip rationale must mention 'polarity mismatch'"
        )

    def test_skill_offers_as_is_option(self):
        """Step 7.5 must offer marking done with `--skip-verify --note` as a third option."""
        text = _skill_text()
        match = re.search(
            r"##\s+Step\s+7\.5.*?(?=\n##\s+Steps?\s+8|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None
        section = match.group(0)
        assert re.search(r"--skip-verify\s+--note", section), (
            "Step 7.5 must show the `--skip-verify --note` form for the as-is option"
        )

    def test_auto_mode_default_is_skip(self):
        """Step 7.5 must specify that auto mode defaults to skip (not silent skip-verify)."""
        text = _skill_text()
        match = re.search(
            r"##\s+Step\s+7\.5.*?(?=\n##\s+Steps?\s+8|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None
        section = match.group(0)
        # Auto mode must explicitly default to skip, not as-is/skip-verify.
        assert re.search(
            r"auto mode.*default.*\*\*skip\*\*", section, re.IGNORECASE | re.DOTALL
        ), (
            "Step 7.5 must instruct auto mode to default to **skip** to avoid burying the polarity signal"
        )


class TestPolarityConventionDocumented:
    def test_skill_has_polarity_convention_subsection(self):
        """The skill must document the polarity convention as a named subsection."""
        text = _skill_text()
        assert re.search(
            r"###\s+Failing Test Polarity Convention", text
        ), "Skill must contain the 'Failing Test Polarity Convention' subsection"

    def test_convention_states_both_invariants(self):
        """The convention must spell out both polarity invariants explicitly."""
        text = _skill_text()
        match = re.search(
            r"###\s+Failing Test Polarity Convention.*?(?=\n##\s|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None, "Convention subsection must be locatable"
        section = match.group(0)
        assert re.search(r"nonzero against the broken", section, re.IGNORECASE), (
            "Convention must state the broken-state invariant (exits nonzero on broken)"
        )
        assert re.search(r"exits 0 against the fixed", section, re.IGNORECASE), (
            "Convention must state the fixed-state invariant (exits 0 on fixed)"
        )

    def test_convention_calls_out_assertion_polarity_pitfall(self):
        """The convention must name the assertion-polarity patterns that trigger the bug."""
        text = _skill_text()
        match = re.search(
            r"###\s+Failing Test Polarity Convention.*?(?=\n##\s|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None
        section = match.group(0)
        assert "test -z" in section, "Convention must call out `test -z` as an assertion-style pitfall"
        assert re.search(r"leading\s+`?!`?", section, re.IGNORECASE), (
            "Convention must call out leading `!` as an assertion-style pitfall"
        )

    def test_step_41_exit_branches_cross_reference_step_75(self):
        """Step 4.1's exit branches must mention that Step 7.5 re-runs the spec post-fix."""
        text = _skill_text()
        # Isolate Step 4.1's body up to the next top-level section.
        match = re.search(
            r"##\s+Step\s+4\.1:.*?(?=\n##\s+Step\s+4\.5|\Z)",
            text,
            re.DOTALL,
        )
        assert match is not None, "Step 4.1 section must be present"
        section = match.group(0)
        # Both exit-nonzero and exit-0 branches must reference Step 7.5 by name.
        assert section.count("Step 7.5") >= 2, (
            "Step 4.1's exit branches must each cross-reference Step 7.5 (got "
            f"{section.count('Step 7.5')} mentions)"
        )


class TestIssueTemplateConvention:
    def test_template_requires_exit_0_after_fix(self):
        """Issue template must state the post-fix exit-0 requirement."""
        text = _template_text()
        assert re.search(r"exit 0.*once the bug is fixed", text, re.IGNORECASE), (
            "Issue template must state that the failing test must exit 0 once the bug is fixed"
        )

    def test_template_calls_out_assertion_polarity(self):
        """Issue template must warn against assertion-style polarity."""
        text = _template_text()
        assert "test -z" in text, "Issue template must mention `test -z` as a pitfall"
        # The wrap-with-! workaround must be shown so authors have an out.
        assert re.search(r"!\s*\(\s*test\s+-z", text), (
            "Issue template must show the `! ( test -z ... )` wrap as the inversion workaround"
        )
