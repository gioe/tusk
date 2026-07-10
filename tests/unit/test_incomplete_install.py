import os
import shutil
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_upgrade_from_incomplete_install_reports_actionable_diagnostic(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    shutil.copy(os.path.join(REPO_ROOT, "bin", "tusk"), stub / "tusk")
    shutil.copy(os.path.join(REPO_ROOT, "bin", "tusk_loader.py"), stub / "tusk_loader.py")

    result = subprocess.run(
        [str(stub / "tusk"), "upgrade", "--no-commit"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert f"incomplete tusk install at {stub}" in result.stderr
    assert "missing tusk-upgrade.py" in result.stderr
    assert "correct PATH" in result.stderr
    assert "can't open file" not in result.stderr


def test_arbitrary_dispatch_from_incomplete_install_is_guarded(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    shutil.copy(os.path.join(REPO_ROOT, "bin", "tusk"), stub / "tusk")
    shutil.copy(os.path.join(REPO_ROOT, "bin", "tusk_loader.py"), stub / "tusk_loader.py")

    result = subprocess.run(
        [str(stub / "tusk"), "task-list"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert "incomplete tusk install" in result.stderr
    assert "can't open file" not in result.stderr


def test_incomplete_install_still_supports_version(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    shutil.copy(os.path.join(REPO_ROOT, "bin", "tusk"), stub / "tusk")
    shutil.copy(os.path.join(REPO_ROOT, "bin", "tusk_loader.py"), stub / "tusk_loader.py")

    result = subprocess.run(
        [str(stub / "tusk"), "version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "tusk version 0"
