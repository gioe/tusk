"""Unit tests for tusk-commit.py MANIFEST drift auto-recovery (Issue #674).

Verifies that when `tusk lint` blocks a commit solely due to Rule 18 / Rule 19
(MANIFEST drift), `tusk commit` runs `tusk generate-manifest` once internally,
re-runs lint, and — on success — appends MANIFEST and .claude/tusk-manifest.json
to the staging set so they ride along with the user's original file list.
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


def _argv(tmp_path, task_id="42", message="my message", files=None, extra=None):
    config = tmp_path / "config.json"
    config.write_text("{}")
    if files is None:
        (tmp_path / "somefile.py").write_text("")
    return (
        [str(tmp_path), str(config), task_id, message]
        + (files or ["somefile.py"])
        + (extra or [])
    )


def _make_completed(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


_LINT_OUTPUT_RULE18_ONLY = """\
Rule 18: MANIFEST drift from source tree
  WARN — 2 violations
  MANIFEST: missing '.claude/hooks/foo.sh' (in source tree but not in MANIFEST)
  Fix: run `tusk generate-manifest`.

=== Summary: 2 violations across 1 rule ===
"""

_LINT_OUTPUT_RULE18_AND_19 = """\
Rule 18: MANIFEST drift from source tree
  WARN — 1 violations
  MANIFEST: missing '.claude/hooks/foo.sh' (in source tree but not in MANIFEST)
  Fix: run `tusk generate-manifest`.

Rule 19: .claude/tusk-manifest.json out of sync with MANIFEST
  WARN — 1 violations
  MANIFEST has '.claude/hooks/foo.sh' but .claude/tusk-manifest.json does not
  Fix: run `tusk generate-manifest`.

=== Summary: 2 violations across 2 rules ===
"""

_LINT_OUTPUT_RULE6_AND_18 = """\
Rule 6: Done with incomplete acceptance criteria
  WARN — 1 violations
  task 99 closed with 2 incomplete criteria

Rule 18: MANIFEST drift from source tree
  WARN — 1 violations
  MANIFEST: missing '.claude/hooks/foo.sh' (in source tree but not in MANIFEST)
  Fix: run `tusk generate-manifest`.

=== Summary: 2 violations across 2 rules ===
"""

_LINT_OUTPUT_RULE18_AND_ADVISORY = """\
Rule 18: MANIFEST drift from source tree
  WARN — 1 violations
  MANIFEST: missing '.claude/hooks/foo.sh' (in source tree but not in MANIFEST)
  Fix: run `tusk generate-manifest`.

Rule 23: CLAUDE.md exceeds line limit (advisory)
  WARN [ADVISORY] — 1 violations
  CLAUDE.md is 250 lines (limit 200)

=== Summary: 1 violation across 1 rule ===
"""


class TestBlockingLintRulesParser:
    """Verify _blocking_lint_rules() correctly identifies the blocking rule set."""

    def test_empty_input(self):
        mod = _load_module()
        assert mod._blocking_lint_rules("") == set()

    def test_only_rule18(self):
        mod = _load_module()
        assert mod._blocking_lint_rules(_LINT_OUTPUT_RULE18_ONLY) == {18}

    def test_rule18_and_rule19(self):
        mod = _load_module()
        assert mod._blocking_lint_rules(_LINT_OUTPUT_RULE18_AND_19) == {18, 19}

    def test_rule18_with_unrelated_blocking_rule(self):
        mod = _load_module()
        assert mod._blocking_lint_rules(_LINT_OUTPUT_RULE6_AND_18) == {6, 18}

    def test_advisory_rules_are_skipped(self):
        mod = _load_module()
        assert mod._blocking_lint_rules(_LINT_OUTPUT_RULE18_AND_ADVISORY) == {18}

    def test_manifest_lint_rules_constant(self):
        mod = _load_module()
        assert mod._MANIFEST_LINT_RULES == {18, 19}


class TestManifestAutoRecover:
    """Lint blocks on Rule 18 → tusk commit auto-runs generate-manifest and retries."""

    def test_rule18_only_triggers_recovery(self, tmp_path, capsys):
        """Lint fails with only Rule 18, regen succeeds, lint clean → exit 0,
        MANIFEST and .claude/tusk-manifest.json appended to staged paths."""
        mod = _load_module()
        argv = _argv(tmp_path)

        lint_calls = []
        regen_calls = []
        staged_paths = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                if len(lint_calls) == 1:
                    return _make_completed(1, stdout=_LINT_OUTPUT_RULE18_ONLY)
                return _make_completed(0)
            if args[1:2] == ["generate-manifest"]:
                regen_calls.append(args)
                return _make_completed(0, stdout="Wrote MANIFEST and ... (5 entries)")
            if args[:2] == ["git", "add"]:
                # Capture the staged file list (everything after "--")
                if "--" in args:
                    sep = args.index("--")
                    staged_paths.extend(args[sep + 1:])
                return _make_completed(0)
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[branch bbb222] msg")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="bbb222\n")
            if args[:3] == ["git", "ls-files"]:
                return _make_completed(0, stdout="")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert len(lint_calls) == 2, "lint should run exactly twice (initial + retry)"
        assert len(regen_calls) == 1, "generate-manifest should run exactly once"

        # Both manifest paths must be staged alongside the user's file
        manifest_abs = os.path.join(str(tmp_path), "MANIFEST")
        tusk_manifest_abs = os.path.join(str(tmp_path), ".claude/tusk-manifest.json")
        assert manifest_abs in staged_paths
        assert tusk_manifest_abs in staged_paths

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "MANIFEST drift detected" in combined

    def test_rule18_and_rule19_triggers_recovery(self, tmp_path, capsys):
        """Both manifest rules firing together is still recoverable."""
        mod = _load_module()
        argv = _argv(tmp_path)

        lint_calls = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                if len(lint_calls) == 1:
                    return _make_completed(1, stdout=_LINT_OUTPUT_RULE18_AND_19)
                return _make_completed(0)
            if args[1:2] == ["generate-manifest"]:
                return _make_completed(0, stdout="ok")
            if args[:2] == ["git", "add"]:
                return _make_completed(0)
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[branch bbb222] msg")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="bbb222\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert len(lint_calls) == 2

    def test_unrelated_blocking_rule_blocks_recovery(self, tmp_path, capsys):
        """Lint fails with Rule 6 + Rule 18 → recovery NOT triggered → exit 6."""
        mod = _load_module()
        argv = _argv(tmp_path)

        lint_calls = []
        regen_calls = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                return _make_completed(1, stdout=_LINT_OUTPUT_RULE6_AND_18)
            if args[1:2] == ["generate-manifest"]:
                regen_calls.append(args)
                return _make_completed(0)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 6
        assert len(lint_calls) == 1, "lint should run once and not retry"
        assert regen_calls == [], "generate-manifest must not run when other rules fire"
        captured = capsys.readouterr()
        assert "MANIFEST drift detected" not in (captured.out + captured.err)

    def test_generate_manifest_failure_aborts(self, tmp_path, capsys):
        """Recovery candidate detected, but generate-manifest itself fails → exit 6,
        no second lint call (no infinite loop)."""
        mod = _load_module()
        argv = _argv(tmp_path)

        lint_calls = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                return _make_completed(1, stdout=_LINT_OUTPUT_RULE18_ONLY)
            if args[1:2] == ["generate-manifest"]:
                return _make_completed(1, stderr="not in source repo")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 6
        assert len(lint_calls) == 1, "lint must not retry when generate-manifest fails"
        captured = capsys.readouterr()
        # Original Rule 18 output should be surfaced when recovery fails
        assert "MANIFEST drift" in (captured.out + captured.err)

    def test_lint_still_fails_after_recovery_aborts(self, tmp_path, capsys):
        """Recovery runs but lint still fails on retry → exit 6, no third attempt."""
        mod = _load_module()
        argv = _argv(tmp_path)

        lint_calls = []
        regen_calls = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                return _make_completed(1, stdout=_LINT_OUTPUT_RULE18_ONLY)
            if args[1:2] == ["generate-manifest"]:
                regen_calls.append(args)
                return _make_completed(0)
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 6
        assert len(lint_calls) == 2, "lint should run exactly twice — no third attempt"
        assert len(regen_calls) == 1, "generate-manifest should run exactly once"

    def test_skip_lint_bypasses_recovery_path(self, tmp_path, capsys):
        """--skip-lint bypasses lint entirely — recovery never runs."""
        mod = _load_module()
        argv = _argv(tmp_path, extra=["--skip-lint"])

        lint_calls = []
        regen_calls = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                return _make_completed(1, stdout=_LINT_OUTPUT_RULE18_ONLY)
            if args[1:2] == ["generate-manifest"]:
                regen_calls.append(args)
                return _make_completed(0)
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[branch bbb222] msg")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="bbb222\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert lint_calls == [], "--skip-lint must skip lint entirely"
        assert regen_calls == [], "--skip-lint must skip recovery entirely"

    def test_clean_lint_unchanged_behavior(self, tmp_path, capsys):
        """Lint passes first try → no regen, no extra lint calls, exit 0."""
        mod = _load_module()
        argv = _argv(tmp_path)

        lint_calls = []
        regen_calls = []

        def fake_run(args, **kwargs):
            if args[1:3] == ["lint", "--quiet"]:
                lint_calls.append(args)
                return _make_completed(0)
            if args[1:2] == ["generate-manifest"]:
                regen_calls.append(args)
                return _make_completed(0)
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[branch bbb222] msg")
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="bbb222\n")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0
        assert len(lint_calls) == 1, "lint runs exactly once on the clean path"
        assert regen_calls == [], "generate-manifest must not run on the clean path"
