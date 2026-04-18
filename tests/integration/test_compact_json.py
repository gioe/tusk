"""Compact-JSON-by-default contract for bin/tusk stdout commands.

Agents consume JSON from stdout; indentation is wasted bytes on the hot path.
These tests lock the default to single-line compact JSON (no leading-indent
lines, no post-separator spaces) and prove that TUSK_PRETTY=1 and --pretty
both restore indented output for humans.
"""

import json
import os
import sqlite3
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _insert_task(db_file: str) -> int:
    conn = sqlite3.connect(db_file)
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, complexity, priority_score)"
            " VALUES ('compact json fixture', 'exercises tusk task-get output shape',"
            " 'To Do', 'feature', 'Medium', 'S', 50)"
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _run(args, env_overrides=None):
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [TUSK_BIN, *args], capture_output=True, text=True, env=env, check=False
    )


class TestTaskGetCompactJson:
    def test_task_get_stdout_is_compact_single_line_json(self, db_path):
        task_id = _insert_task(str(db_path))
        result = _run(["task-get", str(task_id)], env_overrides={"TUSK_DB": str(db_path)})
        assert result.returncode == 0, result.stderr

        payload = result.stdout.rstrip("\n")
        # Compact JSON has no embedded newlines — exactly one line of output.
        assert "\n" not in payload, (
            f"expected single-line compact JSON, got:\n{payload}"
        )
        # No leading-indent lines (indent=2 starts nested fields with two spaces).
        assert not payload.startswith("  "), "output must not start with indent whitespace"
        # Tight separators: compact mode uses \",\" and \":\" (no trailing spaces).
        assert '", "' not in payload, "compact output must not contain ', ' separators"
        assert '": ' not in payload, "compact output must not contain ': ' separators"
        # Result must still be parseable JSON with the expected shape.
        parsed = json.loads(payload)
        assert parsed["task"]["id"] == task_id
        assert "acceptance_criteria" in parsed
        assert "task_progress" in parsed

    def test_tusk_pretty_env_restores_indented_output(self, db_path):
        task_id = _insert_task(str(db_path))
        result = _run(
            ["task-get", str(task_id)],
            env_overrides={"TUSK_DB": str(db_path), "TUSK_PRETTY": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert "\n  " in result.stdout, "TUSK_PRETTY=1 must restore indented JSON"
        json.loads(result.stdout)  # still valid

    def test_pretty_flag_restores_indented_output(self, db_path):
        task_id = _insert_task(str(db_path))
        result = _run(
            ["task-get", str(task_id), "--pretty"],
            env_overrides={"TUSK_DB": str(db_path)},
        )
        assert result.returncode == 0, result.stderr
        assert "\n  " in result.stdout, "--pretty must restore indented JSON"
        json.loads(result.stdout)


class TestConfigCompactJson:
    def test_config_dict_key_is_compact_by_default(self, db_path):
        result = _run(["config", "review"], env_overrides={"TUSK_DB": str(db_path)})
        assert result.returncode == 0, result.stderr
        payload = result.stdout.rstrip("\n")
        assert "\n" not in payload, f"expected compact config JSON, got:\n{payload}"
        assert '", "' not in payload
        json.loads(payload)

    def test_config_dict_key_respects_pretty_flag(self, db_path):
        result = _run(
            ["config", "review", "--pretty"], env_overrides={"TUSK_DB": str(db_path)}
        )
        assert result.returncode == 0, result.stderr
        assert "\n  " in result.stdout
        json.loads(result.stdout)
