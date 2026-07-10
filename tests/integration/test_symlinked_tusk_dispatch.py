"""Helper dispatch resolves from the real tusk binary behind a symlink."""

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed_install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "install"
    shutil.copytree(REPO_ROOT / "bin", install / "bin")

    machine_bin = tmp_path / "machine-bin"
    machine_bin.mkdir()
    link = machine_bin / "tusk"
    link.symlink_to(Path("..") / "install" / "bin" / "tusk")
    return install, link


def _run(link: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        TUSK_DB=str(link.parent.parent / "tasks.db"),
        TUSK_STATE_DIR=str(link.parent.parent / "state"),
        TUSK_QUIET="1",
    )
    return subprocess.run(
        [str(link), *args],
        cwd=link.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_symlinked_upgrade_dispatches_real_install_helper(tmp_path: Path) -> None:
    install, link = _seed_install(tmp_path)

    result = _run(link, "upgrade", "--help")

    assert result.returncode == 0, result.stderr
    assert "Upgrade tusk from GitHub" in result.stdout
    assert str(link.parent / "tusk-upgrade.py") not in result.stderr
    assert (install / "bin" / "tusk-upgrade.py").is_file()


def test_symlinked_non_upgrade_helper_dispatches_from_real_install(
    tmp_path: Path,
) -> None:
    _, link = _seed_install(tmp_path)

    result = _run(link, "typed-criteria-build", "--help")

    assert result.returncode == 0, result.stderr
    assert "Emit a properly-escaped --typed-criteria JSON string" in result.stdout


def test_direct_upgrade_dispatch_is_unchanged(tmp_path: Path) -> None:
    install, _ = _seed_install(tmp_path)

    result = _run(install / "bin" / "tusk", "upgrade", "--help")

    assert result.returncode == 0, result.stderr
    assert "Upgrade tusk from GitHub" in result.stdout
