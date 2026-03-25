"""Unit tests for rule2_sql_not_equal in tusk-lint.py.

Covers markdown skip, shell violations, and the SQL-keyword heuristic.
"""

import importlib.util
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def populate_root(root: str, files: dict[str, str]) -> None:
    """Write {rel_path: content} entries into an existing root directory."""
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)


# ── Markdown files are skipped ─────────────────────────────────────────


class TestMarkdownSkipped:
    def test_skill_md_with_sql_not_equal_no_violation(self):
        """SKILL.md example text with != in SQL context must not fire."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "skills/my-skill/SKILL.md": (
                        "Use `<>` instead of `!=`:\n"
                        "```sql\n"
                        "WHERE status != 'Done'\n"
                        "```\n"
                    )
                },
            )
            assert lint.rule2_sql_not_equal(root) == []

    def test_skills_internal_md_no_violation(self):
        """Markdown in skills-internal/ must also be skipped."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {
                    "skills-internal/retro/SKILL.md": (
                        "AND status != 'To Do' -- example\n"
                    )
                },
            )
            # skills-internal is not in the scanned dirs, so this is already skipped;
            # the important thing is no crash and no violation.
            assert lint.rule2_sql_not_equal(root) == []

    def test_readme_md_no_violation(self):
        """README.md is already exempt via exempt_patterns."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(root, {"README.md": "WHERE col != 'val'\n"})
            assert lint.rule2_sql_not_equal(root) == []

    def test_any_md_in_skills_dir_no_violation(self):
        """Any .md file under skills/ is skipped, not just SKILL.md."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(root, {"skills/foo/FOCUS.md": "AND foo != 'bar'\n"})
            assert lint.rule2_sql_not_equal(root) == []


# ── Shell files still fire ─────────────────────────────────────────────


class TestShellViolations:
    def test_sh_file_with_sql_not_equal_fires(self):
        """.sh file in skills/ with != in SQL context must be flagged."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {"skills/my-skill/run.sh": "sqlite3 \"$DB\" \"WHERE status != 'Done'\"\n"},
            )
            violations = lint.rule2_sql_not_equal(root)
            assert len(violations) == 1
            assert "run.sh" in violations[0]

    def test_sh_file_without_sql_keyword_no_violation(self):
        """!= in a shell comparison (no SQL keyword) must not fire."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(
                root,
                {"skills/my-skill/run.sh": 'if [ "$x" != "y" ]; then echo ok; fi\n'},
            )
            assert lint.rule2_sql_not_equal(root) == []

    def test_sh_file_in_bin_fires(self):
        """.sh file in bin/ with != in SQL context must be flagged."""
        with tempfile.TemporaryDirectory() as root:
            populate_root(root, {"bin/helper.sh": "AND status != 'Done'\n"})
            violations = lint.rule2_sql_not_equal(root)
            assert len(violations) == 1
