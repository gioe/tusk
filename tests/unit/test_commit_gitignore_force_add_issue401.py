"""Regression test for GitHub Issue #401.

tusk commit should succeed when committing a new file that lives in a
gitignored directory that already has force-tracked siblings (e.g.
.claude/skills/).  Previously, git add would exit non-zero for any file in
an ignored directory, and tusk commit would bail out with exit code 3 instead
of retrying with git add -f.

The fix: when git add fails and git check-ignore -v confirms the file is
ignored, tusk commit automatically retries with git add -f for those files
and falls through to the commit step on success.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_completed(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestGitignoreForceAdd:
    """tusk commit must auto-retry with git add -f for gitignored files."""

    def test_gitignored_file_retried_with_force(self, tmp_path):
        """Exit 0 when git add fails due to gitignore and git add -f succeeds."""
        mod = _load_module()

        # Create the file in a simulated .claude/skills/ directory
        skill_dir = tmp_path / ".claude" / "skills" / "new-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# New Skill\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        rel_path = ".claude/skills/new-skill/SKILL.md"
        argv = [str(tmp_path), str(config), "401", "add new-skill skill file", rel_path]

        # git add fails because .claude/ is gitignored;
        # git check-ignore -v confirms the rule;
        # git add -f succeeds on retry.
        gitignore_stderr = (
            "The following paths are ignored by one of your .gitignore files:\n"
            f"{rel_path}\n"
            "hint: Use -f if you really want to add them."
        )
        gitignore_rule = f".gitignore:1:.claude/\t{rel_path}"

        side_effects = [
            _make_completed(0),                           # tusk lint
            _make_completed(1, stderr=gitignore_stderr),  # git add (fails — gitignored)
            _make_completed(0, stdout=gitignore_rule),    # git check-ignore -v (confirms rule)
            _make_completed(0),                           # git add -f (retry succeeds)
            _make_completed(0, stdout="abc401\n"),        # git rev-parse HEAD (pre)
            _make_completed(0, stdout="[main abc401] [TASK-401] add new-skill skill file\n"),
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, (
            f"Expected exit 0 when git add -f retry succeeds for gitignored file, got {rc}"
        )

    def test_gitignored_file_force_add_also_fails_exits_3(self, tmp_path):
        """Exit 3 when git add -f also fails — still surfaces error and hints."""
        mod = _load_module()

        skill_dir = tmp_path / ".claude" / "skills" / "new-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# New Skill\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        rel_path = ".claude/skills/new-skill/SKILL.md"
        argv = [str(tmp_path), str(config), "401", "add new-skill skill file", rel_path]

        gitignore_stderr = (
            "The following paths are ignored by one of your .gitignore files:\n"
            f"{rel_path}"
        )
        gitignore_rule = f".gitignore:1:.claude/\t{rel_path}"

        side_effects = [
            _make_completed(0),                              # tusk lint
            _make_completed(1, stderr=gitignore_stderr),     # git add (fails)
            _make_completed(0, stdout=gitignore_rule),       # git check-ignore -v
            _make_completed(1, stderr="error: permission denied"),  # git add -f (also fails)
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, (
            f"Expected exit 3 when git add -f also fails, got {rc}"
        )

    def test_non_gitignored_failure_unchanged(self, tmp_path):
        """Non-gitignore git add failures still exit 3 without retrying."""
        mod = _load_module()

        target = tmp_path / "fix.py"
        target.write_text("# fix\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        argv = [str(tmp_path), str(config), "401", "fix something", "fix.py"]

        # git add fails for a non-gitignore reason; check-ignore returns exit 1 (not ignored)
        side_effects = [
            _make_completed(0),                                   # tusk lint
            _make_completed(1, stderr="error: sparse checkout"),  # git add (non-gitignore fail)
            _make_completed(1),                                   # git check-ignore (not ignored)
        ]

        with patch("subprocess.run", side_effect=side_effects), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, (
            f"Expected exit 3 for non-gitignore git add failure, got {rc}"
        )
