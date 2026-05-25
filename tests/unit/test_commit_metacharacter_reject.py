"""Regression tests for the commit-message metacharacter guard (issue #881).

Confirms tusk-commit.py rejects commit messages containing shell-substitution
metacharacters (backticks, $(...), ${...}, $VAR) before any git or sqlite
subprocess runs. The guard exists because zsh and bash expand those patterns
BEFORE tusk sees the argv — TASK-464 demonstrated that a literal backticked
tusk command inside a double-quoted message arg got executed by zsh and the
JSON output was substituted into the commit message, shipping the corruption
to origin (commit 984ca1a on main).
"""

import importlib.util
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _argv(tmp_path, args):
    config = tmp_path / "config.json"
    config.write_text("{}")
    return [str(tmp_path), str(config)] + args


class TestValidateMessageMetacharacters:
    """Direct unit tests of the validation helper."""

    def setup_method(self):
        self.mod = _load_module()

    @pytest.mark.parametrize(
        "message",
        [
            "plain message",
            "Bump VERSION to 985 and update CHANGELOG for issue #874",
            "Multi-line message\nwith newline",
            "Has $1 positional reference",
            "Price is $5.99",
            "Email gioematt@gmail.com",
            "Numbers: 1, 2, 3",
        ],
    )
    def test_safe_messages_pass(self, message):
        ok, diagnostic = self.mod._validate_message_metacharacters(message)
        assert ok is True
        assert diagnostic == ""

    @pytest.mark.parametrize(
        "message,expected_class",
        [
            ("Has a `backtick` here", "backtick"),
            ("Run cmd $(echo hi) and stop", "$(...) command substitution"),
            ("Use ${VAR} for var", "${...} variable substitution"),
            ("Reference $PATH variable", "variable substitution"),
            ("Use $_private here", "variable substitution"),
            ("trailing backtick `", "backtick"),
        ],
    )
    def test_metachar_messages_rejected(self, message, expected_class):
        ok, diagnostic = self.mod._validate_message_metacharacters(message)
        assert ok is False
        assert expected_class in diagnostic
        assert "shell-substitution metacharacter" in diagnostic
        assert "single-quoting" in diagnostic or "single quotes" in diagnostic

    def test_diagnostic_includes_offset(self):
        ok, diagnostic = self.mod._validate_message_metacharacters("abc`def")
        assert ok is False
        assert "position 3" in diagnostic

    def test_diagnostic_includes_message_repr(self):
        msg = "Has a `backtick` here"
        ok, diagnostic = self.mod._validate_message_metacharacters(msg)
        assert ok is False
        assert repr(msg) in diagnostic


class TestRunCommitRejectsMetacharBeforeSubprocess:
    """End-to-end through main(): metachar guard fires before any subprocess.

    The patch.object(subprocess, "run", ...) sentinel asserts that no subprocess
    runs at all when the guard rejects the message — the boundary-guard property
    (criterion 2145).
    """

    def test_backtick_message_exits_nonzero_with_diagnostic(self, tmp_path, capsys):
        mod = _load_module()
        (tmp_path / "file.txt").write_text("change")

        subprocess_called = []

        def explode(*args, **kwargs):
            subprocess_called.append(args)
            raise AssertionError(
                "subprocess.run was invoked despite metachar guard rejection"
            )

        argv = _argv(tmp_path, ["42", "Test `echo HACKED` message", "file.txt"])
        with patch.object(subprocess, "run", side_effect=explode):
            ret = mod.main(argv)

        captured = capsys.readouterr()
        assert ret == 1
        assert subprocess_called == []
        assert "shell-substitution metacharacter" in captured.err
        assert "backtick" in captured.err

    def test_command_substitution_rejected(self, tmp_path, capsys):
        mod = _load_module()
        (tmp_path / "file.txt").write_text("change")

        def explode(*args, **kwargs):
            raise AssertionError("subprocess.run invoked despite guard")

        argv = _argv(tmp_path, ["42", "Run $(rm -rf .) on shutdown", "file.txt"])
        with patch.object(subprocess, "run", side_effect=explode):
            ret = mod.main(argv)

        captured = capsys.readouterr()
        assert ret == 1
        assert "$(...) command substitution" in captured.err

    def test_braced_variable_rejected(self, tmp_path, capsys):
        mod = _load_module()
        (tmp_path / "file.txt").write_text("change")

        def explode(*args, **kwargs):
            raise AssertionError("subprocess.run invoked despite guard")

        argv = _argv(tmp_path, ["42", "Echo ${HOME} value", "file.txt"])
        with patch.object(subprocess, "run", side_effect=explode):
            ret = mod.main(argv)

        captured = capsys.readouterr()
        assert ret == 1
        assert "${...} variable substitution" in captured.err

    def test_bare_dollar_var_rejected(self, tmp_path, capsys):
        mod = _load_module()
        (tmp_path / "file.txt").write_text("change")

        def explode(*args, **kwargs):
            raise AssertionError("subprocess.run invoked despite guard")

        argv = _argv(tmp_path, ["42", "Print $HOME value", "file.txt"])
        with patch.object(subprocess, "run", side_effect=explode):
            ret = mod.main(argv)

        captured = capsys.readouterr()
        assert ret == 1
        assert "variable substitution" in captured.err

    def test_safe_message_proceeds_past_guard(self, tmp_path, capsys):
        """A safe message must not be blocked by the new guard — the guard's
        absence-of-false-positives property. The commit still fails later (no
        real repo / lint / etc.) — what matters is that subprocess.run IS
        invoked, i.e. the guard did not short-circuit on the message itself.
        """
        mod = _load_module()
        (tmp_path / "file.txt").write_text("change")

        called_subprocess = []

        def fake_run(*args, **kwargs):
            called_subprocess.append(args)
            r = MagicMock(spec=subprocess.CompletedProcess)
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        argv = _argv(tmp_path, ["42", "Safe message without metachars", "file.txt"])
        with patch.object(subprocess, "run", side_effect=fake_run):
            mod.main(argv)

        captured = capsys.readouterr()
        assert called_subprocess, (
            "guard incorrectly rejected a safe message — subprocess.run never ran"
        )
        assert "shell-substitution metacharacter" not in captured.err
