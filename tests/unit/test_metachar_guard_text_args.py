"""Regression tests for the shell-substitution metacharacter guard on task text
args (issue #1106).

Issue #881 added a guard to tusk commit that rejects backticks, $(...), ${...},
and bare $IDENT in commit messages because zsh/bash expand them before tusk sees
the argv — silently corrupting stored content. Issue #1106 extends the same
guard to the other text-taking surfaces: tusk task-insert (summary + inline
description + criterion text), tusk task-update (--summary + --description), and
tusk criteria add (criterion text). The regex + diagnostic now live in a shared
helper (reject_shell_metacharacters) in tusk-git-helpers.py so all surfaces stay
in lockstep.

The guard rejects rather than auto-escapes, and fires BEFORE any DB write (and,
for criteria add, after the task-exists check but before the insert). The
--description-file path on task-insert reads the file directly and is immune, so
file-sourced descriptions are intentionally exempt. Typed-criterion specs (and
file-type verification specs) are shell code by design and are NOT checked.
"""

import importlib.util
import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(filename, modname):
    if BIN not in sys.path:
        sys.path.insert(0, BIN)
    spec = importlib.util.spec_from_file_location(modname, os.path.join(BIN, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────── shared helper ───────────────────────────


class TestRejectShellMetacharacters:
    """Direct unit tests of the shared guard in tusk-git-helpers.py."""

    def setup_method(self):
        self.mod = _load("tusk-git-helpers.py", "tusk_git_helpers_mc")

    @pytest.mark.parametrize(
        "text",
        [
            "plain text",
            "Bump VERSION to 985 and update CHANGELOG",
            "Has $1 positional reference",
            "Price is $5.99",
            "Email gioematt@gmail.com",
            "Numbers: 1, 2, 3",
            "Multi-line\ntext",
        ],
    )
    def test_safe_text_passes(self, text):
        ok, diagnostic = self.mod.reject_shell_metacharacters(text, subject="task description")
        assert ok is True
        assert diagnostic == ""

    @pytest.mark.parametrize(
        "text,expected_class",
        [
            ("has a `backtick`", "backtick"),
            ("run $(echo hi) now", "$(...) command substitution"),
            ("use ${VAR} here", "${...} variable substitution"),
            ("reference $PATH var", "variable substitution"),
            ("use $_private here", "variable substitution"),
            ("trailing backtick `", "backtick"),
        ],
    )
    def test_metachar_text_rejected(self, text, expected_class):
        ok, diagnostic = self.mod.reject_shell_metacharacters(text, subject="task summary")
        assert ok is False
        assert expected_class in diagnostic
        assert "shell-substitution metacharacter" in diagnostic

    def test_diagnostic_includes_subject(self):
        ok, diagnostic = self.mod.reject_shell_metacharacters("`x`", subject="criterion text")
        assert ok is False
        assert "criterion text contains" in diagnostic
        assert "  criterion text: " in diagnostic

    def test_diagnostic_includes_offset_and_repr(self):
        text = "abc`def"
        ok, diagnostic = self.mod.reject_shell_metacharacters(text, subject="task summary")
        assert ok is False
        assert "position 3" in diagnostic
        assert repr(text) in diagnostic

    def test_default_remedy_present(self):
        ok, diagnostic = self.mod.reject_shell_metacharacters("`x`", subject="task summary")
        assert "rewrite without the metacharacter" in diagnostic

    def test_custom_remedy_honored(self):
        ok, diagnostic = self.mod.reject_shell_metacharacters(
            "`x`", subject="commit message", remedy="Fix: do the custom thing"
        )
        assert "Fix: do the custom thing" in diagnostic
        assert "rewrite without the metacharacter" not in diagnostic


# ─────────────────────────── task-insert ───────────────────────────


class TestTaskInsertMetacharGuard:
    """tusk task-insert rejects metachars in summary / inline description /
    criterion text before any DB access; --description-file is exempt."""

    def setup_method(self):
        self.mod = _load("tusk-task-insert.py", "tusk_task_insert_mc")

    def _argv(self, tmp_path, args):
        return [str(tmp_path / "tasks.db"), str(tmp_path / "config.json")] + args

    def _assert_blocked_before_db(self, tmp_path, capsys, args, subject):
        """Guard must fire before load_config (the first DB-bound step)."""
        load_calls = []
        argv = self._argv(tmp_path, args)
        with patch.object(self.mod, "load_config", side_effect=lambda *a, **k: load_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert load_calls == [], "guard did not short-circuit before load_config"
        assert subject in err
        assert "shell-substitution metacharacter" in err

    def test_backtick_summary_blocked(self, tmp_path, capsys):
        self._assert_blocked_before_db(
            tmp_path, capsys,
            ["summary with `x`", "plain description", "--criteria", "ok"],
            "task summary",
        )

    def test_dollar_var_description_blocked(self, tmp_path, capsys):
        self._assert_blocked_before_db(
            tmp_path, capsys,
            ["plain summary", "description with $HOME", "--criteria", "ok"],
            "task description",
        )

    def test_metachar_in_criteria_text_blocked(self, tmp_path, capsys):
        self._assert_blocked_before_db(
            tmp_path, capsys,
            ["plain summary", "plain description", "--criteria", "check $(rm -rf .)"],
            "criterion text",
        )

    def test_metachar_in_typed_criteria_text_blocked(self, tmp_path, capsys):
        self._assert_blocked_before_db(
            tmp_path, capsys,
            ["plain summary", "plain description",
             "--typed-criteria", '{"text":"bad `x`","type":"manual"}'],
            "criterion text",
        )

    def test_safe_summary_and_description_reach_config_load(self, tmp_path):
        """Safe text passes the guard and execution reaches load_config."""
        argv = self._argv(tmp_path, ["price is $5.99 ref $1", "plain $1 desc", "--criteria", "ok"])
        with patch.object(self.mod, "load_config", side_effect=RuntimeError("reached load_config")):
            with pytest.raises(RuntimeError, match="reached load_config"):
                self.mod.main(argv)

    def test_description_file_content_is_exempt(self, tmp_path):
        """Backtick content supplied via --description-file is NOT checked."""
        df = tmp_path / "body.md"
        df.write_text("description with `backticks` and $(cmd) is fine via file\n")
        argv = self._argv(tmp_path, ["safe summary", "--description-file", str(df), "--criteria", "ok"])
        with patch.object(self.mod, "load_config", side_effect=RuntimeError("reached load_config")):
            with pytest.raises(RuntimeError, match="reached load_config"):
                self.mod.main(argv)


# ─────────────────────────── task-update ───────────────────────────


class TestTaskUpdateMetacharGuard:
    def setup_method(self):
        self.mod = _load("tusk-task-update.py", "tusk_task_update_mc")

    def _argv(self, tmp_path, args):
        return [str(tmp_path / "tasks.db"), str(tmp_path / "config.json")] + args

    def test_backtick_description_blocked(self, tmp_path, capsys):
        load_calls = []
        argv = self._argv(tmp_path, ["5", "--description", "new `desc`"])
        with patch.object(self.mod, "load_config", side_effect=lambda *a, **k: load_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert load_calls == []
        assert "task description" in err
        assert "shell-substitution metacharacter" in err

    def test_dollar_var_summary_blocked(self, tmp_path, capsys):
        load_calls = []
        argv = self._argv(tmp_path, ["5", "--summary", "renamed $THING"])
        with patch.object(self.mod, "load_config", side_effect=lambda *a, **k: load_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert load_calls == []
        assert "task summary" in err

    def test_safe_summary_reaches_config_load(self, tmp_path):
        argv = self._argv(tmp_path, ["5", "--summary", "plain renamed summary"])
        with patch.object(self.mod, "load_config", side_effect=RuntimeError("reached load_config")):
            with pytest.raises(RuntimeError, match="reached load_config"):
                self.mod.main(argv)


# ─────────────────────────── criteria add ───────────────────────────


class TestCriteriaAddMetacharGuard:
    def setup_method(self):
        self.mod = _load("tusk-criteria.py", "tusk_criteria_mc")

    def _make_db(self, tmp_path):
        db = tmp_path / "tasks.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO tasks (id) VALUES (5)")
        conn.execute(
            "CREATE TABLE acceptance_criteria ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, criterion TEXT, "
            "source TEXT, criterion_type TEXT, verification_spec TEXT, "
            "is_completed INTEGER DEFAULT 0, is_deferred INTEGER DEFAULT 0, "
            "deferred_reason TEXT, updated_at TEXT)"
        )
        conn.commit()
        conn.close()
        return str(db)

    _CONFIG = {"criterion_types": ["manual", "code", "test", "file"]}

    def test_metachar_criterion_text_blocked(self, tmp_path, capsys):
        db_path = self._make_db(tmp_path)
        args = SimpleNamespace(task_id=5, text="criterion with ${VAR}", type="manual",
                               source="original", spec=None)
        ret = self.mod.cmd_add(args, db_path, self._CONFIG)
        err = capsys.readouterr().err
        assert ret == 1
        assert "criterion text" in err
        assert "shell-substitution metacharacter" in err
        # Nothing was inserted.
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM acceptance_criteria").fetchone()[0]
        conn.close()
        assert count == 0

    def test_safe_criterion_text_inserts(self, tmp_path, capsys):
        db_path = self._make_db(tmp_path)
        args = SimpleNamespace(task_id=5, text="a plain criterion with $1 and $5.99",
                               type="manual", source="original", spec=None)
        ret = self.mod.cmd_add(args, db_path, self._CONFIG)
        assert ret == 0
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM acceptance_criteria").fetchone()[0]
        conn.close()
        assert count == 1

    def test_spec_with_metachar_is_exempt(self, tmp_path, capsys):
        """The verification spec is shell code by design — metachars there do
        NOT block, only the criterion text is checked."""
        db_path = self._make_db(tmp_path)
        args = SimpleNamespace(task_id=5, text="plain file criterion",
                               type="file", source="original", spec="bin/tusk")
        ret = self.mod.cmd_add(args, db_path, self._CONFIG)
        assert ret == 0


# ─────────────────────────── progress ───────────────────────────


class TestProgressMetacharGuard:
    """tusk progress rejects metachars in --note and --next-steps before any DB
    access (issue #1107)."""

    def setup_method(self):
        self.mod = _load("tusk-progress.py", "tusk_progress_mc")

    def _argv(self, tmp_path, args):
        return [str(tmp_path / "tasks.db"), str(tmp_path / "config.json")] + args

    def _assert_blocked_before_db(self, tmp_path, capsys, args, subject):
        """Guard must fire before get_connection (the first DB-bound step)."""
        conn_calls = []
        argv = self._argv(tmp_path, args)
        with patch.object(self.mod, "get_connection", side_effect=lambda *a, **k: conn_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert conn_calls == [], "guard did not short-circuit before get_connection"
        assert subject in err
        assert "shell-substitution metacharacter" in err

    def test_backtick_note_blocked(self, tmp_path, capsys):
        self._assert_blocked_before_db(
            tmp_path, capsys, ["5", "--note", "ran `id` here"], "progress note"
        )

    def test_dollar_var_next_steps_blocked(self, tmp_path, capsys):
        self._assert_blocked_before_db(
            tmp_path, capsys, ["5", "--next-steps", "deploy to $STAGE"], "progress next-steps"
        )

    def test_safe_note_and_next_steps_reach_get_connection(self, tmp_path):
        argv = self._argv(tmp_path, ["5", "--note", "price $5.99 ref $1", "--next-steps", "ship it"])
        with patch.object(self.mod, "get_connection", side_effect=RuntimeError("reached get_connection")):
            with pytest.raises(RuntimeError, match="reached get_connection"):
                self.mod.main(argv)


# ─────────────────────────── context add ───────────────────────────


class TestContextAddMetacharGuard:
    """tusk context add rejects metachars in --content before any DB access
    (issue #1107)."""

    def setup_method(self):
        self.mod = _load("tusk-context.py", "tusk_context_mc")

    def _argv(self, tmp_path, args):
        return [str(tmp_path / "tasks.db"), str(tmp_path / "config.json")] + args

    def test_metachar_content_blocked(self, tmp_path, capsys):
        conn_calls = []
        argv = self._argv(tmp_path, ["add", "5", "--type", "memory", "--content", "value is $(whoami)"])
        with patch.object(self.mod, "get_connection", side_effect=lambda *a, **k: conn_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert conn_calls == [], "guard did not short-circuit before get_connection"
        assert "context content" in err
        assert "shell-substitution metacharacter" in err

    def test_safe_content_reaches_get_connection(self, tmp_path):
        argv = self._argv(tmp_path, ["add", "5", "--type", "memory", "--content", "plain $1 note"])
        with patch.object(self.mod, "get_connection", side_effect=RuntimeError("reached get_connection")):
            with pytest.raises(RuntimeError, match="reached get_connection"):
                self.mod.main(argv)


# ─────────────────────────── jot ───────────────────────────


class TestJotMetacharGuard:
    """tusk jot rejects metachars in the category positional and the note arg
    before the INSERT (note: issue #1107; category: issue #1108). get_connection
    opens before the guard, so the relevant boundary is write_jot (the actual DB
    write)."""

    def setup_method(self):
        self.mod = _load("tusk-jot.py", "tusk_jot_mc")

    def _argv(self, tmp_path, args):
        return [str(tmp_path / "tasks.db"), str(tmp_path / "config.json")] + args

    def test_metachar_category_blocked(self, tmp_path, capsys):
        write_calls = []
        argv = self._argv(tmp_path, ["write", "cat`id`", "plain note here"])
        with patch.object(self.mod, "write_jot", side_effect=lambda *a, **k: write_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert write_calls == [], "guard did not short-circuit before write_jot"
        assert "jot category" in err
        assert "shell-substitution metacharacter" in err

    def test_metachar_note_blocked(self, tmp_path, capsys):
        write_calls = []
        argv = self._argv(tmp_path, ["write", "friction", "broke on ${VAR}"])
        with patch.object(self.mod, "write_jot", side_effect=lambda *a, **k: write_calls.append(1)):
            ret = self.mod.main(argv)
        err = capsys.readouterr().err
        assert ret == 1
        assert write_calls == [], "guard did not short-circuit before write_jot"
        assert "jot note" in err
        assert "shell-substitution metacharacter" in err

    def test_safe_category_and_note_reach_write_jot(self, tmp_path):
        # A plain label category (with safe punctuation) and a note carrying only
        # a benign positional ($1) both pass the guard and reach write_jot.
        argv = self._argv(tmp_path, ["write", "test-flake", "plain note about $1"])
        with patch.object(self.mod, "write_jot", side_effect=RuntimeError("reached write_jot")):
            with pytest.raises(RuntimeError, match="reached write_jot"):
                self.mod.main(argv)


# ─────────────────────────── review ───────────────────────────


class TestReviewMetacharGuard:
    """tusk review add-comment / resolve / approve / request-changes reject
    metachars in their text args before any DB access (issue #1107)."""

    def setup_method(self):
        self.mod = _load("tusk-review.py", "tusk_review_mc")

    def _assert_blocked_before_db(self, capsys, call, subject):
        conn_calls = []
        with patch.object(self.mod, "get_connection", side_effect=lambda *a, **k: conn_calls.append(1)):
            ret = call()
        err = capsys.readouterr().err
        assert ret == 1
        assert conn_calls == [], "guard did not short-circuit before get_connection"
        assert subject in err
        assert "shell-substitution metacharacter" in err

    def test_add_comment_metachar_blocked(self, tmp_path, capsys):
        args = SimpleNamespace(review_id=1, comment="bad `code`", file=None,
                               line_start=None, line_end=None, category=None, severity=None)
        self._assert_blocked_before_db(
            capsys, lambda: self.mod.cmd_add_comment(args, "db", "cfg"), "review comment"
        )

    def test_resolve_note_metachar_blocked(self, tmp_path, capsys):
        args = SimpleNamespace(resolution="fixed", note="done via $PATH", comment_id=1)
        self._assert_blocked_before_db(
            capsys, lambda: self.mod.cmd_resolve(args, "db"), "review note"
        )

    def test_approve_note_metachar_blocked(self, tmp_path, capsys):
        args = SimpleNamespace(review_id=1, note="good $(rm -rf .)", model=None,
                               cost_dollars=None, tokens_in=None, tokens_out=None)
        self._assert_blocked_before_db(
            capsys, lambda: self.mod.cmd_approve(args, "db"), "review note"
        )

    def test_request_changes_note_metachar_blocked(self, tmp_path, capsys):
        args = SimpleNamespace(review_id=1, note="fix ${THING}", model=None,
                               cost_dollars=None, tokens_in=None, tokens_out=None)
        self._assert_blocked_before_db(
            capsys, lambda: self.mod.cmd_request_changes(args, "db"), "review note"
        )

    def test_add_comment_safe_reaches_get_connection(self):
        args = SimpleNamespace(review_id=1, comment="plain comment $1", file=None,
                               line_start=None, line_end=None, category=None, severity=None)
        with patch.object(self.mod, "get_connection", side_effect=RuntimeError("reached get_connection")):
            with pytest.raises(RuntimeError, match="reached get_connection"):
                self.mod.cmd_add_comment(args, "db", "cfg")

    def test_approve_empty_note_is_exempt(self):
        """An empty-string note (the 'clear note' sentinel) carries no metachar
        and must pass the guard."""
        args = SimpleNamespace(review_id=1, note="", model=None,
                               cost_dollars=None, tokens_in=None, tokens_out=None)
        with patch.object(self.mod, "get_connection", side_effect=RuntimeError("reached get_connection")):
            with pytest.raises(RuntimeError, match="reached get_connection"):
                self.mod.cmd_approve(args, "db")
