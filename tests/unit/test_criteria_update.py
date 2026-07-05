"""Unit tests for tusk criteria update."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bin" / "tusk-criteria.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_criteria_update", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_db(tmp_path):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE acceptance_criteria ("
        "id INTEGER PRIMARY KEY, task_id INTEGER, criterion TEXT, "
        "criterion_type TEXT, verification_spec TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(id, task_id, criterion, criterion_type, verification_spec) "
        "VALUES (1, 5, 'original criterion', 'manual', NULL)"
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _args(**overrides):
    values = {
        "criterion_id": 1,
        "text": None,
        "criterion_type": None,
        "verification_spec": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _criterion_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, task_id, criterion, criterion_type, verification_spec "
        "FROM acceptance_criteria WHERE id = 1"
    ).fetchone()
    conn.close()
    return dict(row)


def test_update_text_reframes_criterion_in_place(tmp_path, capsys):
    mod = _load_module()
    db_path = _make_db(tmp_path)

    ret = mod.cmd_update(_args(text="reframed criterion text"), db_path, {})

    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == 1
    assert out["criterion"] == "reframed criterion text"
    row = _criterion_row(db_path)
    assert row == {
        "id": 1,
        "task_id": 5,
        "criterion": "reframed criterion text",
        "criterion_type": "manual",
        "verification_spec": None,
    }


def test_update_text_combines_with_type_and_spec(tmp_path, capsys):
    mod = _load_module()
    db_path = _make_db(tmp_path)
    config = {"criterion_types": ["manual", "code", "test", "file"]}

    ret = mod.cmd_update(
        _args(
            text="code criterion text",
            criterion_type="code",
            verification_spec="python3 -m pytest tests/unit/test_criteria_update.py",
        ),
        db_path,
        config,
    )

    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["criterion"] == "code criterion text"
    assert out["criterion_type"] == "code"
    assert out["verification_spec"] == "python3 -m pytest tests/unit/test_criteria_update.py"
    row = _criterion_row(db_path)
    assert row["criterion"] == "code criterion text"
    assert row["criterion_type"] == "code"
    assert row["verification_spec"] == "python3 -m pytest tests/unit/test_criteria_update.py"


def test_update_text_rejects_shell_metacharacters(tmp_path, capsys):
    mod = _load_module()
    db_path = _make_db(tmp_path)

    ret = mod.cmd_update(_args(text="reframe with ${VAR}"), db_path, {})

    assert ret == 1
    err = capsys.readouterr().err
    assert "criterion text" in err
    assert "shell-substitution metacharacter" in err
    assert _criterion_row(db_path)["criterion"] == "original criterion"
