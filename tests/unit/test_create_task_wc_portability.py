"""Regression test for issue #889: /create-task generated portable-broken
code-criterion specs using ``wc -l | grep -q "^N$"``.

BSD wc on macOS prints leading whitespace before the count (``       3`` vs
GNU's ``3``), so the strict ``^N$`` anchor never matches on Darwin. The
criterion ends up semantically satisfied but blocked at auto-verify time,
forcing ``tusk criteria done <cid> --skip-verify``.

skills/create-task/SKILL.md now carries an explicit portability section
warning against the anti-pattern and showing at least one portable
alternative. This test pins both surfaces so a future edit cannot
silently drop the guidance without flagging in CI.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "create-task", "SKILL.md")


def _skill_body() -> str:
    with open(SKILL_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestCreateTaskWcPortabilityGuidance:

    def test_skill_flags_wc_grep_anti_pattern(self):
        """The SKILL must explicitly name the failing pattern so the
        generator can spot it on review. We pin a substring stable
        across minor phrasing changes — the literal anti-pattern shape."""
        body = _skill_body()
        assert "wc -l" in body and "grep -q" in body and "#889" in body, (
            "SKILL.md must reference the wc -l | grep -q anti-pattern and "
            "issue #889 so future generations can find it"
        )

    def test_skill_calls_out_bsd_macos_split(self):
        """The portability hazard is specifically BSD vs GNU wc — name
        both so readers know why the anti-pattern fails."""
        body = _skill_body()
        assert "BSD" in body, "SKILL.md must name BSD as the failure mode"
        assert "macOS" in body, "SKILL.md must mention macOS as the host class"

    def test_skill_documents_portable_alternative(self):
        """At least one portable form must be in the SKILL so the
        generator has something concrete to emit instead. We check for
        the canonical whitespace-strip idiom; awk and diff alternatives
        are nice-to-haves but the tr -d form is the minimum bar."""
        body = _skill_body()
        assert "tr -d '[:space:]'" in body or 'tr -d "[:space:]"' in body, (
            "SKILL.md must document the whitespace-strip portable form"
        )

    def test_skill_pins_issue_889_context(self):
        """Cross-references to the original incident (TASK-474 / #889)
        survive future rewrites. Without this pin the section can drift
        into generic prose that loses the BSD-wc concrete shape."""
        body = _skill_body()
        assert "TASK-474" in body, (
            "SKILL.md must reference the original incident TASK-474"
        )
