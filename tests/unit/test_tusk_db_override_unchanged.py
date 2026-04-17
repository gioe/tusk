"""Regression: TUSK_DB override path is unchanged by the active-project pin.

Verifies that the pre-existing TUSK_DB escape hatch (used by migration/test
flows) still redirects DB_PATH, still suppresses the cross-repo drift warning,
and still causes task-start to skip the active-projects registry write. These
behaviors are load-bearing for `tests/conftest.py::db_path` and `docs/MIGRATIONS.md`.
"""

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def test_tusk_db_overrides_db_path(tmp_path):
    """TUSK_DB points DB_PATH at the override, bypassing CWD-based resolution."""
    override = tmp_path / "override.db"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TUSK_DB": str(override)}
    result = _run([TUSK_BIN, "path"], cwd=str(tmp_path), env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(override)


def test_tusk_db_suppresses_drift_warning(tmp_path):
    """With TUSK_DB set, no cross-repo warning fires even when the registry lists
    a different project.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "active-projects").write_text(f"{REPO_ROOT}\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TUSK_STATE_DIR": str(state_dir),
        "TUSK_DB": str(tmp_path / "any.db"),
    }
    result = _run([TUSK_BIN, "path"], cwd=str(tmp_path), env=env)
    assert result.returncode == 0
    assert "warning" not in result.stderr.lower()


def test_register_active_project_noop_when_tusk_db_set(tmp_path, monkeypatch):
    """_register_active_project() must be a no-op when TUSK_DB is set.

    The TUSK_DB override is untied to a real repo root, so registering it
    would pollute the registry with paths that have no meaningful CWD match.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tusk_task_start", os.path.join(REPO_ROOT, "bin", "tusk-task-start.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    registry = tmp_path / "active-projects"
    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()

    monkeypatch.setenv("TUSK_DB", "/tmp/override.db")
    monkeypatch.setenv("TUSK_ACTIVE_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("TUSK_REPO_ROOT", str(fake_root))

    mod._register_active_project()

    assert not registry.exists(), (
        f"Registry written despite TUSK_DB override: {registry.read_text()}"
    )


def test_register_active_project_writes_when_tusk_db_unset(tmp_path, monkeypatch):
    """Sanity check: _register_active_project does write the registry when
    TUSK_DB is not set. Pairs with the above no-op test.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tusk_task_start", os.path.join(REPO_ROOT, "bin", "tusk-task-start.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    registry = tmp_path / "active-projects"
    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()

    monkeypatch.delenv("TUSK_DB", raising=False)
    monkeypatch.setenv("TUSK_ACTIVE_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("TUSK_REPO_ROOT", str(fake_root))

    mod._register_active_project()

    assert registry.exists()
    assert os.path.realpath(str(fake_root)) in registry.read_text()
