"""Unit tests for rule23_claude_md_size in tusk-lint.py.

Covers the boundary at CLAUDE_MD_LINE_LIMIT (currently 400) and the
source-repo-only guard. Also asserts the advisory message names both
remediation paths (tusk conventions add for behavioral conventions; docs/
extraction for reference detail) so the message stays in sync with the
broadened cap (issue #667).
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


def _make_root(tmp_dir, claude_md_lines):
    """Create a fake project root with tusk/config.json, CLAUDE.md, bin/tusk."""
    os.makedirs(os.path.join(tmp_dir, "tusk"), exist_ok=True)
    os.makedirs(os.path.join(tmp_dir, "bin"), exist_ok=True)
    open(os.path.join(tmp_dir, "tusk", "config.json"), "w").close()
    open(os.path.join(tmp_dir, "bin", "tusk"), "w").close()
    if claude_md_lines is not None:
        with open(os.path.join(tmp_dir, "CLAUDE.md"), "w") as fh:
            fh.write("\n".join(["x"] * claude_md_lines) + "\n")
    return tmp_dir


class TestRule23Boundary:
    def test_at_limit_no_violation(self):
        """CLAUDE.md exactly at the cap does not trigger an advisory."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp, lint.CLAUDE_MD_LINE_LIMIT)
            assert lint.rule23_claude_md_size(tmp) == []

    def test_one_over_limit_triggers_violation(self):
        """CLAUDE.md one line over the cap triggers exactly one advisory."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp, lint.CLAUDE_MD_LINE_LIMIT + 1)
            violations = lint.rule23_claude_md_size(tmp)
        assert len(violations) == 1
        assert "CLAUDE.md" in violations[0]
        assert str(lint.CLAUDE_MD_LINE_LIMIT + 1) in violations[0]
        assert str(lint.CLAUDE_MD_LINE_LIMIT) in violations[0]

    def test_well_under_limit_no_violation(self):
        """A short CLAUDE.md is silent."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp, 50)
            assert lint.rule23_claude_md_size(tmp) == []


class TestRule23Message:
    def test_message_names_both_remediation_paths(self):
        """The advisory must name tusk conventions add AND docs/ extraction.

        Issue #667: when the cap was 300, the message recommended only
        'tusk conventions add', but most CLAUDE.md growth is reference
        detail (command-table entries, gotcha notes) that belongs in docs/.
        Keep both paths in the message.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp, lint.CLAUDE_MD_LINE_LIMIT + 50)
            violations = lint.rule23_claude_md_size(tmp)
        assert len(violations) == 1
        msg = violations[0]
        assert "tusk conventions add" in msg
        assert "docs/" in msg


class TestRule23Guards:
    def test_no_config_returns_empty(self):
        """Source-repo-only guard: no tusk/config.json → no violations."""
        with tempfile.TemporaryDirectory() as tmp:
            # Don't create tusk/config.json
            os.makedirs(os.path.join(tmp, "bin"), exist_ok=True)
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as fh:
                fh.write("\n".join(["x"] * 1000) + "\n")
            assert lint.rule23_claude_md_size(tmp) == []

    def test_no_claude_md_returns_empty(self):
        """No CLAUDE.md file → silent."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tusk"), exist_ok=True)
            open(os.path.join(tmp, "tusk", "config.json"), "w").close()
            assert lint.rule23_claude_md_size(tmp) == []
