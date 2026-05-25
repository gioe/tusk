"""Integration tests for install.sh project_type auto-detection (TASK-447, issue #854).

install.sh detects project_type from manifest files present at install time:
- package.json                                  → web_app
- pyproject.toml / setup.py / requirements.txt → python_service
- Package.swift / *.xcodeproj / *.xcworkspace  → ios_app

After 'tusk init' completes, the detected type is passed to
'tusk init-write-config --project-type <detected>', which triggers the
WORKTREE_SYMLINK_DEFAULTS auto-seed in tusk-init-write-config.py so
install.sh-only installs ship with an explicit worktree.symlink_files list
rather than relying on TASK-446's runtime canonical-fallback at worktree-
create time.

When no manifest signals are present, install.sh skips the call and behavior
is identical to pre-task install.sh — project_type stays at the default null
and worktree.symlink_files stays at [].
"""

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")


def _run(cmd, cwd, check=True):
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
    )
    if check:
        assert result.returncode == 0, (
            f"command {cmd} failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _setup_consumer(tmp_path, manifest_files=(), manifest_dirs=()):
    """Create a tmp consumer project with the named manifest files and dirs."""
    _run(["git", "init"], tmp_path)
    (tmp_path / ".claude").mkdir()
    for name in manifest_files:
        (tmp_path / name).write_text("{}\n" if name.endswith(".json") else "")
    for name in manifest_dirs:
        (tmp_path / name).mkdir()
    return tmp_path


def _read_config(project_root):
    cfg_path = project_root / "tusk" / "config.json"
    assert cfg_path.exists(), f"tusk/config.json must exist after install: {cfg_path}"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def test_install_detects_web_app_from_package_json(tmp_path):
    """Criterion (a): package.json triggers web_app + node_modules/.env/.env.local seed."""
    _setup_consumer(tmp_path, manifest_files=["package.json"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "web_app"
    assert cfg.get("worktree", {}).get("symlink_files") == [
        "node_modules", ".env", ".env.local",
    ]


def test_install_detects_python_service_from_pyproject(tmp_path):
    """Criterion (b): pyproject.toml triggers python_service + .venv/.env seed."""
    _setup_consumer(tmp_path, manifest_files=["pyproject.toml"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "python_service"
    assert cfg.get("worktree", {}).get("symlink_files") == [".venv", ".env"]


def test_install_detects_python_service_from_setup_py(tmp_path):
    """python_service is detected from setup.py as well as pyproject.toml."""
    _setup_consumer(tmp_path, manifest_files=["setup.py"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "python_service"


def test_install_detects_python_service_from_requirements(tmp_path):
    """python_service is detected from requirements.txt as well as pyproject.toml."""
    _setup_consumer(tmp_path, manifest_files=["requirements.txt"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "python_service"


def test_install_detects_ios_app_from_package_swift(tmp_path):
    """ios_app is detected from Package.swift; no symlink defaults apply."""
    _setup_consumer(tmp_path, manifest_files=["Package.swift"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "ios_app"
    # ios_app is intentionally absent from WORKTREE_SYMLINK_DEFAULTS — no canonical
    # gitignored runtime files for iOS, so the list stays empty.
    assert cfg.get("worktree", {}).get("symlink_files") == []


def test_install_detects_ios_app_from_xcodeproj(tmp_path):
    """ios_app is detected from an .xcodeproj directory."""
    _setup_consumer(tmp_path, manifest_dirs=["MyApp.xcodeproj"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "ios_app"


def test_install_no_signals_leaves_project_type_null(tmp_path):
    """Criterion (c): no manifests → project_type null, symlink_files []."""
    _setup_consumer(tmp_path)
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") is None
    assert cfg.get("worktree", {}).get("symlink_files") == []


def test_install_preserves_existing_project_type(tmp_path):
    """Re-running install.sh after project_type is set must not overwrite it.

    Simulates the /tusk-init customization → install.sh re-run path: the
    user's explicit project_type choice is preserved even when manifest
    signals would have detected something else.
    """
    _setup_consumer(tmp_path, manifest_files=["package.json"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg_path = tmp_path / "tusk" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("project_type") == "web_app"

    cfg["project_type"] = "ios_app"
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")

    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "ios_app", (
        "user's explicit project_type must not be clobbered on install.sh re-run"
    )


def test_install_ios_takes_priority_over_python(tmp_path):
    """When both iOS and Python signals are present, ios_app wins (most-specific)."""
    _setup_consumer(tmp_path, manifest_files=["Package.swift", "pyproject.toml"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "ios_app"


def test_install_python_takes_priority_over_web(tmp_path):
    """When both Python and Node signals are present, python_service wins.

    Most full-stack repos with both package.json and pyproject.toml are
    Python backends with a JS tooling layer; .venv is the runtime artifact
    most expensive to recreate per worktree.
    """
    _setup_consumer(tmp_path, manifest_files=["pyproject.toml", "package.json"])
    _run(["bash", INSTALL_SH], tmp_path)
    cfg = _read_config(tmp_path)
    assert cfg.get("project_type") == "python_service"
