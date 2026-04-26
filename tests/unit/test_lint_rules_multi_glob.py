"""Unit tests for comma-separated multi-glob support in _run_lint_rules.

Rule 5 was added in TASK-181 with file_glob ``**/*.md`` — that broad scope
caused it to self-trigger on CHANGELOG.md whenever an entry quoted the
flagged pattern verbatim. TASK-184 narrowed rule 5 to skill/prompt dirs by
adding comma-separated multi-glob support to _run_lint_rules so a single
rule can target multiple scoped paths.
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


_RULE5_PATTERN = r'echo "\$[A-Z_]+(_JSON|_RANGE|_OUTPUT)?\s*"\s*\|\s*jq'
_FLAGGED_LINE = 'foo=$(echo "$VAR_JSON" | jq .name)\n'


def _make_rule(file_glob: str) -> dict:
    return {
        "id": 5,
        "grep_pattern": _RULE5_PATTERN,
        "file_glob": file_glob,
        "message": "echo pipe to jq is unsafe",
    }


def _populate(root: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)


class TestMultiGlobScoping:
    def test_single_glob_still_works(self):
        """Backwards compat: a single glob with no commas behaves exactly as before."""
        with tempfile.TemporaryDirectory() as root:
            _populate(root, {"skills/foo/SKILL.md": _FLAGGED_LINE})
            violations = lint._run_lint_rules(root, [_make_rule("skills/**/*.md")])
        assert len(violations) == 1
        assert "skills/foo/SKILL.md" in violations[0]

    def test_comma_separated_globs_expand_both_paths(self):
        """A comma-separated file_glob unions matches across all entries."""
        with tempfile.TemporaryDirectory() as root:
            _populate(root, {
                "skills/foo/SKILL.md": _FLAGGED_LINE,
                "codex-prompts/bar.md": _FLAGGED_LINE,
            })
            violations = lint._run_lint_rules(
                root,
                [_make_rule("skills/**/*.md,codex-prompts/**/*.md")],
            )
        joined = "\n".join(violations)
        assert "skills/foo/SKILL.md" in joined
        assert "codex-prompts/bar.md" in joined
        assert len(violations) == 2

    def test_changelog_not_matched_by_scoped_globs(self):
        """The fix: CHANGELOG.md no longer self-triggers when it quotes the pattern."""
        with tempfile.TemporaryDirectory() as root:
            _populate(root, {
                "CHANGELOG.md": _FLAGGED_LINE,
                "README.md": _FLAGGED_LINE,
                "docs/HISTORY.md": _FLAGGED_LINE,
            })
            violations = lint._run_lint_rules(
                root,
                [_make_rule("skills/**/*.md,codex-prompts/**/*.md")],
            )
        assert violations == []

    def test_violation_under_scoped_path_still_fires(self):
        """The narrowed scope must not weaken detection where the rule does apply."""
        with tempfile.TemporaryDirectory() as root:
            _populate(root, {
                "skills/foo/SKILL.md": _FLAGGED_LINE,
                "CHANGELOG.md": _FLAGGED_LINE,  # noise — must be ignored
            })
            violations = lint._run_lint_rules(
                root,
                [_make_rule("skills/**/*.md,codex-prompts/**/*.md")],
            )
        assert len(violations) == 1
        assert "skills/foo/SKILL.md" in violations[0]
        assert "CHANGELOG.md" not in violations[0]

    def test_whitespace_around_entries_is_stripped(self):
        """Comma-separated entries tolerate spaces around each glob."""
        with tempfile.TemporaryDirectory() as root:
            _populate(root, {"skills/foo/SKILL.md": _FLAGGED_LINE})
            violations = lint._run_lint_rules(
                root,
                [_make_rule("  skills/**/*.md  ,  codex-prompts/**/*.md  ")],
            )
        assert len(violations) == 1
        assert "skills/foo/SKILL.md" in violations[0]

    def test_overlapping_globs_dedupe_matches(self):
        """A file matched by two overlapping globs reports once, not twice."""
        with tempfile.TemporaryDirectory() as root:
            _populate(root, {"skills/foo/SKILL.md": _FLAGGED_LINE})
            violations = lint._run_lint_rules(
                root,
                [_make_rule("skills/**/*.md,skills/foo/*.md")],
            )
        assert len(violations) == 1
