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
            task_type TEXT,
            status TEXT DEFAULT 'To Do',
            scope_enforced INTEGER DEFAULT 1
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
    monkeypatch.setattr(
        gh,
        "is_trackable_scope_pattern",
        lambda repo_root, pattern, **kwargs: True,
    )


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


def test_scope_list_surfaces_effective_auto_scope_without_mutating(monkeypatch, capsys):
    conn = _make_conn()
    conn.execute(
        "INSERT INTO tasks (id, summary, description, task_type, scope_enforced) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, f"Touch {DERIVED}", f"Edit {DERIVED}.", "bug", 1),
    )
    conn.commit()
    _patch_derivation(monkeypatch)
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = "1"

    rc = scope_mod.cmd_list(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "id": None,
            "task_id": 1,
            "pattern": DERIVED,
            "source": "auto_derived",
            "reason": "effective fallback from task text; not persisted",
            "locked_at": None,
            "locked_by": None,
            "created_at": None,
        }
    ]
    assert _patterns_by_source(conn, "auto_derived") == set()


def test_scope_list_prefers_persisted_rows_over_effective_fallback(monkeypatch, capsys):
    conn = _make_conn()
    _seed_task(conn)
    _patch_derivation(monkeypatch)
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = "1"

    rc = scope_mod.cmd_list(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    patterns = {(row["pattern"], row["source"]) for row in payload}
    assert (PHANTOM, "auto_derived") in patterns
    assert ("custom/op.py", "operator_declared") in patterns
    assert not any(row["id"] is None for row in payload)


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
        all = False
        include_closed = False

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
        all = False
        include_closed = False

    with pytest.raises(SystemExit) as exc:
        scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert exc.value.code == 1


def _seed_bulk(conn):
    """Three tasks: two open (one with a phantom auto row), one Done.

    Each open task gets a phantom auto_derived row plus an operator_declared row
    so the bulk rebuild has something to remove and something to preserve.
    """
    for tid, status in ((1, "To Do"), (2, "In Progress"), (3, "Done")):
        conn.execute(
            "INSERT INTO tasks (id, summary, description, task_type, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, f"Touch the {DERIVED} file", f"This edits {DERIVED}.", "feature", status),
        )
        _add_scope(conn, tid, PHANTOM, "auto_derived")
        _add_scope(conn, tid, "custom/op.py", "operator_declared")
    conn.commit()


def test_cmd_rederive_all_processes_open_tasks(monkeypatch, capsys):
    conn = _make_conn()
    _seed_bulk(conn)
    _patch_derivation(monkeypatch)
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = None
        all = True
        include_closed = False

    rc = scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    # Default = open tasks only: task 3 (Done) is excluded.
    assert payload["all"] is True
    assert payload["include_closed"] is False
    assert payload["tasks_processed"] == 2
    assert payload["tasks_changed"] == 2
    assert [r["task_id"] for r in payload["results"]] == [1, 2]

    # Each per-task result carries the removed/added/preserved diff, and the
    # rebuild rewrote auto_derived rows while preserving operator_declared rows.
    for r in payload["results"]:
        assert r["removed"] == [PHANTOM]
        assert r["added"] == [DERIVED]
        assert r["auto_derived"] == [DERIVED]
        assert {(p["pattern"], p["source"]) for p in r["preserved"]} == {
            ("custom/op.py", "operator_declared")
        }

    # The Done task's rows were left untouched (not iterated).
    assert {r["pattern"] for r in conn.execute(
        "SELECT pattern FROM task_scope WHERE task_id = 3 AND source = 'auto_derived'"
    ).fetchall()} == {PHANTOM}


def test_cmd_rederive_all_include_closed_processes_done(monkeypatch, capsys):
    conn = _make_conn()
    _seed_bulk(conn)
    _patch_derivation(monkeypatch)
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = None
        all = True
        include_closed = True

    rc = scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["include_closed"] is True
    assert payload["tasks_processed"] == 3
    assert [r["task_id"] for r in payload["results"]] == [1, 2, 3]


def test_cmd_rederive_all_and_task_id_exits_2(monkeypatch, capsys):
    conn = _make_conn()
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = "1"
        all = True
        include_closed = False

    rc = scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_cmd_rederive_neither_exits_1(monkeypatch, capsys):
    conn = _make_conn()
    monkeypatch.setattr(scope_mod, "get_connection", lambda db_path: conn)

    class _Args:
        task_id = None
        all = False
        include_closed = False

    rc = scope_mod.cmd_rederive(_Args(), ":memory:", "/repo/tusk/config.json")
    assert rc == 1
    assert "task_id or --all" in capsys.readouterr().err


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
