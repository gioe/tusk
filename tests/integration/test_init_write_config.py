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


def test_python_service_seeds_symlink_files_default(initialised_project):
    """`--project-type python_service` (without --worktree-symlink-files) seeds
    `worktree.symlink_files` to [".venv", ".env"] — the canonical Python-service
    default. This is the seam that TASK-409 fixes: a fresh consumer install no
    longer ships with an empty list discoverable only via manual config edit."""
    result = _run(initialised_project, "--project-type", "python_service")
    assert result.returncode == 0, f"init-write-config failed:\n{result.stderr}"

    cfg = _read_config(initialised_project)
    assert cfg["worktree"]["symlink_files"] == [".venv", ".env"]


def test_ios_app_does_not_seed_symlink_files(initialised_project):
    """`--project-type ios_app` deliberately stays out of the defaults map —
    iOS projects have no canonical gitignored runtime files to symlink, so the
    existing value must carry forward unchanged."""
    before = _read_config(initialised_project)
    symlinks_before = before["worktree"]["symlink_files"]
    assert symlinks_before == [], "fresh install should start empty"

    result = _run(initialised_project, "--project-type", "ios_app")
    assert result.returncode == 0, f"init-write-config failed:\n{result.stderr}"

    cfg = _read_config(initialised_project)
    assert cfg["worktree"]["symlink_files"] == symlinks_before


def test_explicit_worktree_symlink_files_overrides_default(initialised_project):
    """`--worktree-symlink-files` takes precedence over the project_type
    auto-default — the wizard's interactive prompt path passes the
    user-confirmed value through this flag."""
    result = _run(
        initialised_project,
        "--project-type", "python_service",
        "--worktree-symlink-files", '["node_modules", ".env.local"]',
    )
    assert result.returncode == 0, f"init-write-config failed:\n{result.stderr}"

    cfg = _read_config(initialised_project)
    assert cfg["worktree"]["symlink_files"] == ["node_modules", ".env.local"]


def test_worktree_symlink_files_rejects_invalid_json(initialised_project):
    """Malformed JSON exits with success=false and leaves the existing
    config untouched — matches the validation behavior of every other
    JSON-typed flag in this helper."""
    before = _read_config(initialised_project)

    result = _run(
        initialised_project,
        "--worktree-symlink-files", "not-a-json-array",
    )
    assert result.returncode == 0, "helper returns 0 but reports success=false"
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert "--worktree-symlink-files is not valid JSON" in payload["error"]

    cfg = _read_config(initialised_project)
    assert cfg == before, "config must be untouched on validation failure"


def test_python_service_preserves_existing_symlink_files_customization(initialised_project):
    """A re-run that passes --project-type python_service WITHOUT
    --worktree-symlink-files must not clobber a previously-customized list.
    The auto-default block only seeds when the existing list is missing or
    empty (mirrors project_libs merge semantics — defaults augment, never
    overwrite)."""
    setup = _run(
        initialised_project,
        "--worktree-symlink-files", '["node_modules", ".env.local"]',
    )
    assert setup.returncode == 0, f"setup failed:\n{setup.stderr}"

    result = _run(initialised_project, "--project-type", "python_service")
    assert result.returncode == 0, f"init-write-config failed:\n{result.stderr}"

    cfg = _read_config(initialised_project)
    assert cfg["worktree"]["symlink_files"] == ["node_modules", ".env.local"], (
        "user-customized symlink_files must survive a project_type re-run"
    )


def test_worktree_symlink_files_rejects_non_string_entries(initialised_project):
    """Non-string entries (e.g. ints) are rejected — symlink basenames must
    be strings since they map to path-walking targets in
    `bin/tusk-task-worktree.py`."""
    result = _run(
        initialised_project,
        "--worktree-symlink-files", '[".venv", 42]',
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert "must be a JSON array of strings" in payload["error"]
