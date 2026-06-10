"""Regression tests for the file-type glob-metacharacter warning (issue #1032).

File-type criterion specs are matched with glob.glob at verification time, so
a literal filename like Chivo[wght].ttf silently never matches — the bracket
sequence becomes a character class. task-insert and criteria add must call
out the metacharacters explicitly instead of silently excluding the spec from
the path-does-not-exist warning.
"""

from __future__ import annotations

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

WARNING_MARKER = "contains glob metacharacter"
MISSING_PATH_MARKER = "does not exist at repo root"


def _run(db_path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TUSK_DB": str(db_path)}
    return subprocess.run(
        [TUSK_BIN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _insert_with_spec(db_path, summary: str, spec: str, ctype: str = "file"):
    typed = json.dumps({"text": "spec criterion", "type": ctype, "spec": spec})
    return _run(
        db_path,
        "task-insert",
        summary,
        "--description",
        "glob warning regression body",
        "--typed-criteria",
        typed,
    )


def test_file_type_glob_warning_on_task_insert(db_path):
    result = _insert_with_spec(
        db_path,
        "bracket glob font cleanup",
        "ios/Resources/Fonts/Chivo[wght].ttf",
    )

    assert result.returncode == 0, result.stderr
    assert WARNING_MARKER in result.stderr
    assert "'['" in result.stderr
    assert "ios/Resources/Fonts/Chivo[wght].ttf" in result.stderr
    # Distinct from the path-does-not-exist warning: the metachar spec is
    # excluded from that check, so only the metachar warning fires.
    assert MISSING_PATH_MARKER not in result.stderr


def test_file_type_clean_spec_has_no_glob_warning(db_path):
    result = _insert_with_spec(
        db_path,
        "clean literal file spec",
        "docs/DOMAIN.md",
    )

    assert result.returncode == 0, result.stderr
    assert WARNING_MARKER not in result.stderr


def test_non_file_type_spec_with_glob_chars_not_warned(db_path):
    result = _insert_with_spec(
        db_path,
        "test type spec keeps shell globs",
        "python3 -m pytest tests/unit/test_*.py -q",
        ctype="test",
    )

    assert result.returncode == 0, result.stderr
    assert WARNING_MARKER not in result.stderr


def test_criteria_add_file_type_glob_warning(db_path):
    insert = _run(
        db_path,
        "task-insert",
        "criteria add glob warning host",
        "--description",
        "host task",
        "--criteria",
        "placeholder",
    )
    assert insert.returncode == 0, insert.stderr
    task_id = json.loads(insert.stdout)["task_id"]

    result = _run(
        db_path,
        "criteria",
        "add",
        str(task_id),
        "Variable font removed from bundle",
        "--type",
        "file",
        "--spec",
        "fonts/DMSans[opsz,wght].ttf",
    )

    assert result.returncode == 0, result.stderr
    assert WARNING_MARKER in result.stderr
    assert "'['" in result.stderr
    assert "fonts/DMSans[opsz,wght].ttf" in result.stderr


def test_criteria_add_clean_file_spec_has_no_glob_warning(db_path):
    insert = _run(
        db_path,
        "task-insert",
        "criteria add clean spec host",
        "--description",
        "host task",
        "--criteria",
        "placeholder",
    )
    assert insert.returncode == 0, insert.stderr
    task_id = json.loads(insert.stdout)["task_id"]

    result = _run(
        db_path,
        "criteria",
        "add",
        str(task_id),
        "Doc file present",
        "--type",
        "file",
        "--spec",
        "docs/DOMAIN.md",
    )

    assert result.returncode == 0, result.stderr
    assert WARNING_MARKER not in result.stderr
