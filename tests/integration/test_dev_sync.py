"""Integration tests for `tusk dev-sync` (source-repo dev refresher)."""

import hashlib
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-dev-sync.py")


def _run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture()
def fake_repo(tmp_path):
    """A repo-shaped tree with a fake bin/ and an empty .claude/bin/."""
    (tmp_path / "VERSION").write_text("1234\n")
    (tmp_path / "pricing.json").write_text('{"models":{"example":{"input":1}}}\n')
    src = tmp_path / "bin"
    src.mkdir()
    (src / "tusk").write_text(
        "#!/bin/bash\n"
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"\n'
        'if [[ -f "$SCRIPT_DIR/VERSION" ]]; then\n'
        '  version="$(cat "$SCRIPT_DIR/VERSION")"\n'
        "else\n"
        '  version="$(cat "$INSTALL_DIR/VERSION")"\n'
        "fi\n"
        'echo "tusk version $version"\n'
    )
    os.chmod(src / "tusk", 0o755)
    (src / "tusk-foo.py").write_text("# foo\n")
    (src / "tusk-bar.py").write_text("# bar\n")
    (src / "tusk-lint.py").write_text("# lint v1\n")
    # Stub the underscore-named helpers; the canonical list is imported from
    # the live source repo (the script's own directory), so the iteration
    # picks these up by name.
    (src / "tusk_loader.py").write_text("# loader\n")
    (src / "tusk_skill_filter.py").write_text("# filter\n")
    (src / "tusk_github.py").write_text("# github\n")
    (src / "tusk_underscore_bin_files.py").write_text("# bin-files\n")
    target = tmp_path / ".claude" / "bin"
    target.mkdir(parents=True)
    return tmp_path


def test_dev_sync_copies_bash_entry_python_scripts_and_underscore_files(fake_repo):
    result = _run(str(fake_repo))
    assert result.returncode == 0, result.stderr

    target = fake_repo / ".claude" / "bin"
    assert (target / "VERSION").read_text() == "1234\n"
    assert (target / "pricing.json").read_text() == (
        fake_repo / "pricing.json"
    ).read_text()
    assert (target / "tusk").read_text() == (fake_repo / "bin" / "tusk").read_text()
    assert os.access(str(target / "tusk"), os.X_OK), "executable bit must be preserved"
    assert (target / "tusk-foo.py").read_text() == "# foo\n"
    assert (target / "tusk-bar.py").read_text() == "# bar\n"
    assert (target / "tusk-lint.py").read_text() == "# lint v1\n"
    for name in (
        "tusk_loader.py",
        "tusk_skill_filter.py",
        "tusk_github.py",
        "tusk_underscore_bin_files.py",
    ):
        assert (target / name).is_file(), f"{name} should be copied"


def test_dev_sync_refreshes_lint_hash_sidecar(fake_repo):
    result = _run(str(fake_repo))
    assert result.returncode == 0, result.stderr

    lint_py = fake_repo / ".claude" / "bin" / "tusk-lint.py"
    hash_path = fake_repo / ".claude" / "bin" / "tusk-lint.py.hash"
    expected = hashlib.md5(lint_py.read_bytes()).hexdigest() + "\n"
    assert hash_path.read_text() == expected


def test_dev_sync_dry_run_writes_nothing(fake_repo):
    target = fake_repo / ".claude" / "bin"
    (target / "VERSION").write_text("stale\n")
    before = sorted(p.name for p in target.iterdir())

    result = _run(str(fake_repo), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Would copy" in result.stdout
    assert "  VERSION\n" in result.stdout
    assert "  pricing.json\n" in result.stdout

    after = sorted(p.name for p in target.iterdir())
    assert before == after, "dry-run must not write any files"
    assert (target / "VERSION").read_text() == "stale\n"


def test_dev_sync_refuses_when_source_bin_missing(tmp_path):
    (tmp_path / ".claude" / "bin").mkdir(parents=True)
    result = _run(str(tmp_path))
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    assert "source repo" in result.stderr


def test_dev_sync_refuses_when_claude_bin_missing(tmp_path):
    src = tmp_path / "bin"
    src.mkdir()
    (src / "tusk").write_text("#!/bin/bash\n")
    result = _run(str(tmp_path))
    assert result.returncode == 2
    assert ".claude/bin" in result.stderr


def test_dev_sync_overwrites_stale_target_files(fake_repo):
    target = fake_repo / ".claude" / "bin"
    (target / "VERSION").write_text("old\n")
    (target / "pricing.json").write_text("{}\n")
    (target / "tusk-foo.py").write_text("# stale\n")
    (target / "tusk-lint.py").write_text("# stale lint\n")

    result = _run(str(fake_repo))
    assert result.returncode == 0, result.stderr

    assert (target / "VERSION").read_text() == "1234\n"
    assert (target / "pricing.json").read_text() == (
        fake_repo / "pricing.json"
    ).read_text()
    assert (target / "tusk-foo.py").read_text() == "# foo\n"
    assert (target / "tusk-lint.py").read_text() == "# lint v1\n"
    expected_hash = hashlib.md5(b"# lint v1\n").hexdigest() + "\n"
    assert (target / "tusk-lint.py.hash").read_text() == expected_hash


def test_dev_sync_aligns_source_and_installed_version_commands(fake_repo):
    target = fake_repo / ".claude" / "bin"
    (target / "VERSION").write_text("old\n")

    result = _run(str(fake_repo))
    assert result.returncode == 0, result.stderr

    source_version = subprocess.run(
        [str(fake_repo / "bin" / "tusk"), "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    installed_version = subprocess.run(
        [str(target / "tusk"), "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert source_version.stdout == installed_version.stdout == "tusk version 1234\n"
