"""Regression tests for tusk commit's gitignore retry logic (Issue #401, TASK-88).

Behavior contract:
  * When `git add` fails because a tracked path is blocked by a gitignore
    directory rule (e.g. /tusk/ in .gitignore with tusk/config.json historically
    force-added), tusk commit must retry with `git add -f` and succeed.
  * When `git add` fails on an untracked gitignored path, tusk commit must
    REFUSE to force-add it and surface the rule — otherwise it could silently
    pull in build artifacts, .env files, or other excluded content (TASK-88).
  * Tracked status is decided by `git ls-files --error-unmatch`; the rule text
    comes from `git check-ignore --no-index -v` so it works for tracked paths
    under a gitignored directory too (plain check-ignore skips tracked files).
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
    """tusk commit retries tracked-but-blocked paths with `git add -f`."""

    def test_tracked_file_under_ignored_dir_retried_with_force(self, tmp_path):
        """Exit 0 when git add fails for a tracked file under a gitignored directory."""
        mod = _load_module()

        # Simulate the TASK-88 scenario: /tusk/ in .gitignore, but tusk/config.json
        # was historically force-added at init time.
        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir()
        cfg = tusk_dir / "config.json"
        cfg.write_text('{"a": 1}')

        config = tmp_path / "config.json"
        config.write_text("{}")

        rel_path = "tusk/config.json"
        argv = [str(tmp_path), str(config), "88", "update config", rel_path]

        gitignore_stderr = (
            "The following paths are ignored by one of your .gitignore files:\n"
            "tusk\n"
            "hint: Use -f if you really want to add them."
        )
        gitignore_rule = f".gitignore:1:/tusk/\t{rel_path}"

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                if "-f" in args:
                    return _make_completed(0)
                return _make_completed(1, stderr=gitignore_stderr)
            if args[:3] == ["git", "ls-files", "--error-unmatch"]:
                return _make_completed(0, stdout=rel_path + "\n")
            if args[:4] == ["git", "check-ignore", "--no-index", "-v"]:
                return _make_completed(0, stdout=gitignore_rule)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="abc088\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(
                    0, stdout="[main abc088] [TASK-88] update config\n"
                )
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, (
            f"Expected exit 0 when tracked file force-add succeeds, got {rc}"
        )

    def test_tracked_file_force_add_also_fails_exits_3(self, tmp_path):
        """Exit 3 when `git add -f` also fails — surfaces error and hints."""
        mod = _load_module()

        tusk_dir = tmp_path / "tusk"
        tusk_dir.mkdir()
        cfg = tusk_dir / "config.json"
        cfg.write_text('{"a": 1}')

        config = tmp_path / "config.json"
        config.write_text("{}")

        rel_path = "tusk/config.json"
        argv = [str(tmp_path), str(config), "88", "update config", rel_path]

        gitignore_stderr = (
            "The following paths are ignored by one of your .gitignore files:\n"
            "tusk"
        )
        gitignore_rule = f".gitignore:1:/tusk/\t{rel_path}"

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                if "-f" in args:
                    return _make_completed(1, stderr="error: permission denied")
                return _make_completed(1, stderr=gitignore_stderr)
            if args[:3] == ["git", "ls-files", "--error-unmatch"]:
                return _make_completed(0, stdout=rel_path + "\n")
            if args[:4] == ["git", "check-ignore", "--no-index", "-v"]:
                return _make_completed(0, stdout=gitignore_rule)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, (
            f"Expected exit 3 when git add -f also fails, got {rc}"
        )

    def test_untracked_gitignored_file_refused(self, tmp_path, capfd):
        """Exit 3 without retrying `-f` when the blocked file is untracked (TASK-88)."""
        mod = _load_module()

        # Brand-new file in an untracked ignored path — tusk commit must NOT
        # silently force-add it.
        skill_dir = tmp_path / ".claude" / "skills" / "new-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# New Skill\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        rel_path = ".claude/skills/new-skill/SKILL.md"
        argv = [str(tmp_path), str(config), "88", "add new-skill skill file", rel_path]

        gitignore_stderr = (
            "The following paths are ignored by one of your .gitignore files:\n"
            f"{rel_path}"
        )
        gitignore_rule = f".gitignore:1:.claude/\t{rel_path}"

        force_add_called = {"count": 0}

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                if "-f" in args:
                    force_add_called["count"] += 1
                    return _make_completed(0)
                return _make_completed(1, stderr=gitignore_stderr)
            if args[:3] == ["git", "ls-files", "--error-unmatch"]:
                # Untracked — exit 1 with the canonical git error on stderr.
                return _make_completed(
                    1,
                    stderr=f"error: pathspec '{rel_path}' did not match any file(s) "
                           "known to git",
                )
            if args[:4] == ["git", "check-ignore", "--no-index", "-v"]:
                return _make_completed(0, stdout=gitignore_rule)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, (
            f"Expected exit 3 when untracked gitignored file is refused, got {rc}"
        )
        assert force_add_called["count"] == 0, (
            "Untracked gitignored file must not be force-added silently"
        )
        captured = capfd.readouterr()
        combined = captured.out + captured.err
        assert "Refusing to force-add untracked gitignored file" in combined, (
            f"Expected refusal message in output, got:\n{combined}"
        )

    def test_mixed_tracked_and_untracked_refuses_all(self, tmp_path):
        """A single untracked-ignored file in the batch blocks the whole commit."""
        mod = _load_module()

        tracked = "tusk/config.json"
        untracked = "build/artifact.bin"
        config = tmp_path / "config.json"
        config.write_text("{}")

        (tmp_path / "tusk").mkdir()
        (tmp_path / "tusk" / "config.json").write_text("{}")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "artifact.bin").write_text("bin")

        argv = [str(tmp_path), str(config), "88", "mixed", tracked, untracked]

        stderr = (
            "The following paths are ignored by one of your .gitignore files:\n"
            "tusk\nbuild"
        )

        force_add_called = {"count": 0}

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                if "-f" in args:
                    force_add_called["count"] += 1
                    return _make_completed(0)
                return _make_completed(1, stderr=stderr)
            if args[:3] == ["git", "ls-files", "--error-unmatch"]:
                # Tracked iff the path is tusk/config.json.
                if tracked in args:
                    return _make_completed(0, stdout=tracked + "\n")
                return _make_completed(1, stderr="error: pathspec ... did not match")
            if args[:4] == ["git", "check-ignore", "--no-index", "-v"]:
                # Both paths are under ignored directories.
                path = args[-1]
                if path == tracked:
                    return _make_completed(0, stdout=f".gitignore:1:/tusk/\t{tracked}")
                if path == untracked:
                    return _make_completed(0, stdout=f".gitignore:2:build/\t{untracked}")
                return _make_completed(1)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, (
            f"Expected exit 3 when any blocked file is untracked, got {rc}"
        )
        assert force_add_called["count"] == 0, (
            "No force-add should be attempted when any untracked gitignored file is present"
        )

    def test_non_gitignored_failure_unchanged(self, tmp_path):
        """Non-gitignore git add failures still exit 3 without retrying."""
        mod = _load_module()

        target = tmp_path / "fix.py"
        target.write_text("# fix\n")

        config = tmp_path / "config.json"
        config.write_text("{}")

        argv = [str(tmp_path), str(config), "401", "fix something", "fix.py"]

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return _make_completed(1, stderr="error: sparse checkout")
            if args[:3] == ["git", "ls-files", "--error-unmatch"]:
                return _make_completed(0, stdout="fix.py\n")
            if args[:4] == ["git", "check-ignore", "--no-index", "-v"]:
                return _make_completed(1)  # not ignored
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 3, (
            f"Expected exit 3 for non-gitignore git add failure, got {rc}"
        )
