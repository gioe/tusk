"""Regression coverage for executable unknown-argument suggestions."""

import importlib.util
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _run_tusk(tmp_path, *args):
    db_path = tmp_path / "tasks.db"
    db_path.touch()
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "TUSK_DB": str(db_path),
        "TUSK_NO_BACKUP": "1",
    }
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _load_merge_module():
    spec = importlib.util.spec_from_file_location("tusk_merge_executable_suggestions", MERGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_task_show_suggestion_consumes_alias_verb(tmp_path):
    result = _run_tusk(tmp_path, "task", "show", "3745")

    assert result.returncode != 0
    assert "Did you mean: tusk task-get 3745?" in result.stderr
    assert "tusk task-get show" not in result.stderr


def test_task_get_alias_and_existing_get_shortcut_are_executable(tmp_path):
    task_alias = _run_tusk(tmp_path, "task", "get", "3745")
    existing_shortcut = _run_tusk(tmp_path, "get", "49")

    assert "Did you mean: tusk task-get 3745?" in task_alias.stderr
    assert "Did you mean: tusk task-get 49?" in existing_shortcut.stderr


def test_session_id_suggests_the_supported_merge_option(tmp_path, capsys):
    module = _load_merge_module()

    result = module.main(
        [str(tmp_path / "tasks.db"), str(tmp_path / "config.json"), "3745", "--session-id", "3575"]
    )

    assert result == 1
    assert capsys.readouterr().err.splitlines() == [
        "Error: Unknown argument: --session-id",
        "Did you mean: tusk merge 3745 --session 3575?",
    ]


def test_low_confidence_merge_option_keeps_plain_diagnostic(tmp_path, capsys):
    module = _load_merge_module()

    result = module.main(
        [str(tmp_path / "tasks.db"), str(tmp_path / "config.json"), "3745", "--mystery"]
    )

    assert result == 1
    assert capsys.readouterr().err.splitlines() == ["Error: Unknown argument: --mystery"]
