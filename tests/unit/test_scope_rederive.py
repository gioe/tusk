"""Unit coverage for `tusk scope rederive` (issue #1118).

`scope rederive` re-runs the same `_rederive_auto_scope` path `tusk task-update`
runs on a summary/description edit, but on demand — so operators can rebuild a
task's stale `auto_derived` scope rows (and clear the spurious
`missing_scope_path` context_health_warnings they produce) after the derivation
logic changes, without editing the description.

These tests pin the contract the command relies on:
  * `auto_derived` rows are deleted and rebuilt from the current text;
  * `operator_declared` / `creates` / `unbounded` rows are never touched;
  * the JSON summary reports the removed/added/preserved diff;
  * dropping a phantom `auto_derived` row clears its `missing_scope_path`
    warning (proved against `tusk-task-brief`'s live warning computation).

Derivation internals (`_auto_scope_candidates`, trackable-path validation,
prose-identifier filtering, repo-root resolution) are monkeypatched so the
rebuild is deterministic and the tests stay pure in-memory.
"""

import importlib.util
import json
import os
import sqlite3

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scope_mod = _load("tusk_scope", os.path.join(BIN, "tusk-scope.py"))
brief_mod = _load("tusk_task_brief", os.path.join(BIN, "tusk-task-brief.py"))


PHANTOM = "apps/web/foreign.tsx"   # never derived, does not exist on disk
DERIVED = "VERSION"                # derived from the task text, exists on disk


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            description TEXT,
            task_type TEXT
        );
        CREATE TABLE task_scope (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            pattern TEXT,
            source TEXT,
            reason TEXT,
            locked_at TEXT,
            locked_by TEXT,
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            criterion TEXT,
            verification_spec TEXT
        );
        """
    )
    return conn


def _add_scope(conn, task_id, pattern, source):
    conn.execute(
        "INSERT INTO task_scope (task_id, pattern, source) VALUES (?, ?, ?)",
        (task_id, pattern, source),
    )


def _patch_derivation(monkeypatch):
    """Make `_rederive_auto_scope` derive exactly [DERIVED] from the task text."""
    ti = scope_mod._task_update._task_insert
    gh = scope_mod._task_update._git_helpers
    monkeypatch.setattr(ti, "_repo_root", lambda config_path, **kw: "/repo")
    monkeypatch.setattr(
        ti,
        "_auto_scope_candidates",
        lambda text, **kw: [DERIVED] if DERIVED in (text or "") else [],
    )
    monkeypatch.setattr(ti, "is_prose_identifier_path", lambda path, repo_root: False)
    monkeypatch.setattr(
        ti, "_resolve_auto_derived_scope_pattern", lambda repo_root, path: path
    )
    monkeypatch.setattr(gh, "is_trackable_scope_pattern", lambda repo_root, pattern: True)


def _seed_task(conn, *, unbounded=False):
    conn.execute(
        "INSERT INTO tasks (id, summary, description, task_type) VALUES (?, ?, ?, ?)",
        (1, f"Touch the {DERIVED} file", f"This edits {DERIVED}.", "feature"),
    )
    _add_scope(conn, 1, PHANTOM, "auto_derived")
    _add_scope(conn, 1, "custom/op.py", "operator_declared")
    _add_scope(conn, 1, "future/created.py", "creates")
    if unbounded:
        _add_scope(conn, 1, "**", "unbounded")
    conn.commit()


def _patterns_by_source(conn, source):
    return {
        r["pattern"]
        for r in conn.execute(
            "SELECT pattern FROM task_scope WHERE task_id = 1 AND source = ?",
            (source,),
        ).fetchall()
    }


def test_rederive_rebuilds_auto_and_preserves_non_auto(monkeypatch):
    conn = _make_conn()
    _seed_task(conn)
    _patch_derivation(monkeypatch)

    scope_mod.rederive_auto_scope(conn, 1, "/repo/tusk/config.json")
    conn.commit()

    # auto_derived is rebuilt from the current text: phantom dropped, DERIVED added.
    assert _patterns_by_source(conn, "auto_derived") == {DERIVED}
    # operator_declared / creates rows are untouched.
    assert _patterns_by_source(conn, "operator_declared") == {"custom/op.py"}
    assert _patterns_by_source(conn, "creates") == {"future/created.py"}


def test_rederive_preserves_unbounded_and_drops_auto(monkeypatch):
    conn = _make_conn()
    _seed_task(conn, unbounded=True)
    _patch_derivation(monkeypatch)

    scope_mod.rederive_auto_scope(conn, 1, "/repo/tusk/config.json")
    conn.commit()

    # Unbounded short-circuit: every auto_derived row is removed, none re-added.
    assert _patterns_by_source(conn, "auto_derived") == set()
    assert _patterns_by_source(conn, "unbounded") == {"**"}
    assert _patterns_by_source(conn, "operator_declared") == {"custom/op.py"}


def test_cmd_rederive_emits_removed_added_preserved(monkeypatch, capsys):
    conn = _make_conn()
    _seed_task(conn)
    _patch_derivation(monkeypatch)
    # cmd_rederive opens its own connection via get_connection; hand it the
    # shared in-memory conn (a sqlite3.Connection is a no-close context manager).
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = "1"

    rc = scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == 1
    assert payload["removed"] == [PHANTOM]
    assert payload["added"] == [DERIVED]
    assert payload["auto_derived"] == [DERIVED]
    preserved = {(r["pattern"], r["source"]) for r in payload["preserved"]}
    assert preserved == {("custom/op.py", "operator_declared"), ("future/created.py", "creates")}


def test_cmd_rederive_unknown_task_exits_1(monkeypatch, capsys):
    conn = _make_conn()
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = "999"

    with pytest.raises(SystemExit) as exc:
        scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert exc.value.code == 1


def test_rederive_clears_missing_scope_path_warning(monkeypatch):
    """Dropping a phantom auto_derived row clears its missing_scope_path warning.

    Proved against tusk-task-brief's live warning computation: before rederive
    the phantom path (nonexistent on disk) yields a missing_scope_path warning;
    after rederive the phantom row is gone, so the warning disappears, while the
    rebuilt DERIVED path (which exists on disk) produces none.
    """
    conn = _make_conn()
    _seed_task(conn)
    _patch_derivation(monkeypatch)

    def _phantom_warnings():
        rows = conn.execute(
            "SELECT id, pattern, source FROM task_scope WHERE task_id = 1"
        ).fetchall()
        warnings = brief_mod._missing_scope_warnings(REPO_ROOT, rows)
        return [w for w in warnings if w["details"]["pattern"] == PHANTOM]

    # Before: the phantom auto_derived row raises a missing_scope_path warning.
    assert _phantom_warnings()

    scope_mod.rederive_auto_scope(conn, 1, "/repo/tusk/config.json")
    conn.commit()

    # After: the phantom row is gone, so its warning is cleared.
    assert _phantom_warnings() == []
