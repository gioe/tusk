"""Unit tests for the top-level catch-all in tusk-scope.py (issue #946).

Before this guard, an uncaught exception inside a scope subcommand — e.g. a
transient ``sqlite3.OperationalError: database is locked`` under concurrent
access — propagated out as a bare traceback or a nonzero exit that the
silent-exit guard (bin/tusk:73-95) masked with its generic "exited N with no
diagnostic output" line. The catch-all mirrors the skill-run guard (#785) so
scope failures always carry an actionable stderr message.
"""

import importlib.util
import io
import os
import sqlite3
from contextlib import redirect_stderr

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_scope", os.path.join(REPO_ROOT, "bin", "tusk-scope.py")
)
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)


def _argv(*subcmd_args):
    return ["tusk-scope.py", "/tmp/unused.db", "/tmp/unused-cfg.json", *subcmd_args]


def test_uncaught_exception_emits_diagnostic(monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scope, "cmd_add", boom)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = scope.main(_argv("add", "1", "some/path"))

    assert rc == 1
    out = err.getvalue()
    # Names the subcommand, the exception type, and the message — not silent.
    assert "scope add crashed" in out
    assert "OperationalError" in out
    assert "database is locked" in out


def test_argparse_usage_error_still_propagates(monkeypatch):
    # argparse raises SystemExit, which is not an Exception subclass, so the
    # catch-all must not swallow genuine usage errors.
    with pytest.raises(SystemExit):
        scope.main(_argv("not-a-subcommand"))
