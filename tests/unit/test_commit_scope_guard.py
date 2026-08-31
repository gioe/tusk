"""Focused tests for direct commit-time task-scope enforcement."""

import importlib.util
import json
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit_scope", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path, *, enforced=1, patterns=(), checkpointed=False):
    db_path = tmp_path / "tasks.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                scope_enforced INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE task_scope (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE task_scope_checkpoints (
                task_id INTEGER PRIMARY KEY,
                locked_at TEXT,
                locked_by TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks (id, scope_enforced) VALUES (1, ?)", (enforced,)
        )
        for pattern, source in patterns:
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (1, ?, ?)",
                (pattern, source),
            )
        if checkpointed:
            conn.execute(
                "INSERT INTO task_scope_checkpoints "
                "(task_id, locked_at, locked_by) VALUES (1, 'now', 'test')"
            )
    return str(db_path)


def _config(tmp_path, always_allowed=("VERSION",)):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"scope": {"always_allowed": list(always_allowed)}}),
        encoding="utf-8",
    )
    return str(path)


def _validate(
    mod,
    tmp_path,
    monkeypatch,
    db_path,
    paths,
    *,
    always_allowed=("VERSION",),
):
    monkeypatch.setenv("TUSK_DB", db_path)
    return mod._validate_commit_scope(
        str(tmp_path), _config(tmp_path, always_allowed), 1, list(paths)
    )


def test_rejects_all_undeclared_paths_with_add_guidance(tmp_path, monkeypatch):
    mod = _load_module()
    db_path = _make_db(
        tmp_path, patterns=(("declared.py", "operator_declared"),)
    )

    ok, diagnostic = _validate(
        mod, tmp_path, monkeypatch, db_path, ["outside.py", "other.py"]
    )

    assert ok is False
    assert "outside.py" in diagnostic
    assert "other.py" in diagnostic
    assert "tusk scope add 1 <path>" in diagnostic
    assert "scope expand" not in diagnostic


def test_checkpointed_rejection_recommends_audited_expand(tmp_path, monkeypatch):
    mod = _load_module()
    db_path = _make_db(
        tmp_path,
        patterns=(("declared.py", "operator_declared"),),
        checkpointed=True,
    )

    ok, diagnostic = _validate(
        mod, tmp_path, monkeypatch, db_path, ["outside.py"]
    )

    assert ok is False
    assert "tusk scope expand 1 <path>" in diagnostic


@pytest.mark.parametrize(
    "patterns,paths",
    [
        ((("src", "operator_declared"),), ["src/pkg/code.py"]),
        ((("docs/*.md", "operator_declared"),), ["docs/guide.md"]),
        ((("future/new.py", "creates"),), ["future/new.py"]),
    ],
)
def test_declared_directory_glob_and_creates_patterns_pass(
    tmp_path, monkeypatch, patterns, paths
):
    mod = _load_module()
    db_path = _make_db(tmp_path, patterns=patterns)

    ok, diagnostic = _validate(mod, tmp_path, monkeypatch, db_path, paths)

    assert ok is True
    assert diagnostic == ""


def test_unbounded_always_allowed_and_legacy_tasks_pass(tmp_path, monkeypatch):
    mod = _load_module()

    unbounded = _make_db(tmp_path, patterns=(("**", "unbounded"),))
    assert _validate(mod, tmp_path, monkeypatch, unbounded, ["any/path.py"])[0]

    os.remove(unbounded)
    declared = _make_db(
        tmp_path, patterns=(("declared.py", "operator_declared"),)
    )
    assert _validate(mod, tmp_path, monkeypatch, declared, ["VERSION"])[0]

    os.remove(declared)
    legacy = _make_db(tmp_path, enforced=0)
    assert _validate(mod, tmp_path, monkeypatch, legacy, ["legacy.py"])[0]


def test_explicit_empty_always_allowed_does_not_restore_defaults(
    tmp_path, monkeypatch
):
    mod = _load_module()
    db_path = _make_db(
        tmp_path, patterns=(("declared.py", "operator_declared"),)
    )

    ok, diagnostic = _validate(
        mod,
        tmp_path,
        monkeypatch,
        db_path,
        ["VERSION"],
        always_allowed=(),
    )

    assert ok is False
    assert "VERSION" in diagnostic


def test_enforced_empty_scope_fails_closed(tmp_path, monkeypatch):
    mod = _load_module()
    db_path = _make_db(tmp_path)

    ok, diagnostic = _validate(
        mod, tmp_path, monkeypatch, db_path, ["outside.py"]
    )

    assert ok is False
    assert "has no declared scope" in diagnostic
    assert "tusk scope add 1 <path>" in diagnostic


def test_pre_staged_passenger_is_validated(tmp_path, monkeypatch):
    mod = _load_module()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "passenger.py").write_text("passenger\n", encoding="utf-8")
    subprocess.run(["git", "add", "passenger.py"], cwd=tmp_path, check=True)
    db_path = _make_db(
        tmp_path, patterns=(("declared.py", "operator_declared"),)
    )

    ok, diagnostic = _validate(
        mod, tmp_path, monkeypatch, db_path, ["declared.py"]
    )

    assert ok is False
    assert "passenger.py" in diagnostic


def test_missing_scope_schema_fails_open(tmp_path, monkeypatch):
    mod = _load_module()
    db_path = tmp_path / "old.db"
    sqlite3.connect(db_path).close()

    ok, diagnostic = _validate(
        mod, tmp_path, monkeypatch, str(db_path), ["outside.py"]
    )

    assert ok is True
    assert diagnostic == ""
