"""Integration tests for `tusk init-write-config` (TASK-254).

Direct end-to-end coverage for the merge-and-refresh helper that the
init wizard delegates to. Distinct from `test_init_wizard.py`, which
exercises the full wizard wrapper.
"""

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


@pytest.fixture()
def initialised_project(tmp_path):
    """A git repo + AGENTS.md + freshly-initialised tusk/ DB and config —
    mirrors a Codex install after install.sh has run."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    (tmp_path / "tusk").mkdir()
    db_file = tmp_path / "tusk" / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file)}
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"tusk init failed:\n{result.stderr}"
    return tmp_path


def _run(tmp_path, *args):
    db_file = tmp_path / "tusk" / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file)}
    return subprocess.run(
        [TUSK_BIN, "init-write-config", *args],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _read_config(tmp_path):
    return json.loads((tmp_path / "tusk" / "config.json").read_text())


def test_android_app_project_type_round_trips(initialised_project):
    """`--project-type android_app` persists verbatim into tusk/config.json
    and survives a re-read. android_app has no auto-populated project_libs
    entry today (deferred until a corresponding lib repo exists), so the
    config must carry forward existing project_libs without injecting a
    bogus android_app entry."""
    before = _read_config(initialised_project)
    libs_before = before.get("project_libs") or {}

    result = _run(initialised_project, "--project-type", "android_app")
    assert result.returncode == 0, f"init-write-config failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True

    cfg = _read_config(initialised_project)
    assert cfg["project_type"] == "android_app"
    libs_after = cfg.get("project_libs") or {}
    assert "android_app" not in libs_after, (
        "android_app must NOT be auto-populated in project_libs — "
        "deferred until a corresponding lib repo exists"
    )
    assert libs_after == libs_before, (
        "project_libs must carry forward unchanged when android_app is selected"
    )
