"""Integration tests for the task-insert already-passing warning (issue #1061).

When a typed code/file criterion's verification spec already passes at
insert time, task-insert emits a non-blocking stderr warning so convergent
completion is caught before any worktree is created. The warning never
changes the exit code or the JSON stdout contract.
"""

from __future__ import annotations

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _insert(*typed_criteria):
    argv = [TUSK_BIN, "task-insert", "warn repro", "warn repro description",
            "--priority", "Low", "--complexity", "XS"]
    for tc in typed_criteria:
        argv += ["--typed-criteria", json.dumps(tc)]
    return subprocess.run(argv, capture_output=True, text=True)


def test_warns_when_code_spec_already_passes(db_path):
    result = _insert({"text": "noop passes", "type": "code", "spec": "true"})
    assert result.returncode == 0, result.stderr
    assert "already passes" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["task_id"] > 0


def test_silent_when_code_spec_fails(db_path):
    result = _insert({"text": "noop fails", "type": "code", "spec": "false"})
    assert result.returncode == 0, result.stderr
    assert "already passes" not in result.stderr


def test_warns_when_file_spec_matches(db_path):
    result = _insert({"text": "readme exists", "type": "file", "spec": "README.md"})
    assert result.returncode == 0, result.stderr
    assert "already passes" in result.stderr


def test_test_type_specs_are_excluded(db_path):
    result = _insert({"text": "suite passes", "type": "test", "spec": "true"})
    assert result.returncode == 0, result.stderr
    assert "already passes" not in result.stderr
