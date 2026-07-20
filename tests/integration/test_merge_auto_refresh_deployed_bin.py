"""Integration tests for tusk-merge.py's `_maybe_refresh_deployed_bin` helper.

Issue #863: source-repo fixes to bin/tusk-*.py shipped to origin/main did not
take effect for the rest of the session because the deployed copies under
.claude/bin/ remained stale. The helper auto-refreshes .claude/bin/ at the
end of `tusk merge` when content drift is detected between bin/ and
.claude/bin/.
"""

import importlib.util
import os
import stat
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


@pytest.fixture()
def tusk_merge_module():
    """Load tusk-merge.py as an importable module."""
    # tusk-merge.py uses sys.path.insert(0, ...) to load sibling tusk_loader,
    # so the bin/ directory must be on path before import.
    bin_dir = os.path.join(REPO_ROOT, "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    spec = importlib.util.spec_from_file_location("tusk_merge_under_test", MERGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_fake_tusk_bin(tmp_path):
    """Write a shell script that mimics `tusk dev-sync` in a source repo.

    Records invocations to `<tmp_path>/fake-tusk.log` so the test can assert
    the helper actually invoked it.
    """
    fake = tmp_path / "fake-tusk"
    log = tmp_path / "fake-tusk.log"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{log}"\n'
        "if [[ \"$1\" == \"dev-sync\" ]]; then\n"
        "  src=\"$PWD/bin\"\n"
        "  dst=\"$PWD/.claude/bin\"\n"
        "  if [[ -d \"$src\" && -d \"$dst\" ]]; then\n"
        "    for f in \"$src\"/tusk-*.py; do\n"
        "      [[ -e \"$f\" ]] || continue\n"
        "      cp \"$f\" \"$dst/$(basename \"$f\")\"\n"
        "    done\n"
        "    if [[ -f \"$src/tusk\" ]]; then\n"
        "      cp \"$src/tusk\" \"$dst/tusk\"\n"
        "    fi\n"
        "    if [[ -f \"$PWD/pricing.json\" ]]; then\n"
        "      cp \"$PWD/pricing.json\" \"$dst/pricing.json\"\n"
        "    fi\n"
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(fake), log


def _source_repo_layout(
    tmp_path,
    src_content="version 2",
    dst_content="version 1",
    src_pricing=None,
    dst_pricing=None,
):
    """Create a primary-checkout-shaped layout: bin/, .claude/bin/, tusk/tasks.db."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tusk-foo.py").write_text(src_content)
    (tmp_path / ".claude" / "bin").mkdir(parents=True)
    (tmp_path / ".claude" / "bin" / "tusk-foo.py").write_text(dst_content)
    if src_pricing is not None:
        (tmp_path / "pricing.json").write_text(src_pricing)
    if dst_pricing is not None:
        (tmp_path / ".claude" / "bin" / "pricing.json").write_text(dst_pricing)
    (tmp_path / "tusk").mkdir()
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")  # only the path is used
    return str(tmp_path / "tusk" / "tasks.db")


def test_refreshes_when_drift_detected(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(tmp_path)
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert log.exists(), "fake tusk-bin should have been invoked"
    assert log.read_text().strip() == "dev-sync", "should invoke `tusk dev-sync`"
    assert (tmp_path / ".claude" / "bin" / "tusk-foo.py").read_text() == "version 2", \
        ".claude/bin/ should match bin/ after refresh"
    captured = capsys.readouterr()
    assert "auto-refreshed .claude/bin/" in captured.err
    assert "tusk-foo.py" in captured.err


def test_noop_when_no_drift(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(tmp_path, src_content="same", dst_content="same")
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert not log.exists(), "fake tusk-bin must NOT be invoked when no drift"
    captured = capsys.readouterr()
    assert captured.err == "", "no status line when no drift"


def test_refreshes_when_only_pricing_drifted(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(
        tmp_path,
        src_content="same",
        dst_content="same",
        src_pricing='{"models":{"new":{}}}\n',
        dst_pricing='{"models":{"old":{}}}\n',
    )
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert log.read_text().strip() == "dev-sync"
    assert (tmp_path / ".claude" / "bin" / "pricing.json").read_text() == (
        tmp_path / "pricing.json"
    ).read_text()
    assert "pricing.json" in capsys.readouterr().err


def test_noop_when_pricing_and_bin_match(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(
        tmp_path,
        src_content="same",
        dst_content="same",
        src_pricing='{"models":{}}\n',
        dst_pricing='{"models":{}}\n',
    )
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert not log.exists()
    assert capsys.readouterr().err == ""


def test_noop_when_consumer_install_missing_claude_bin(tmp_path, tusk_merge_module, capsys):
    # No .claude/bin/ — simulates a consumer install or a non-Claude layout.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tusk-foo.py").write_text("version 2")
    (tmp_path / "tusk").mkdir()
    db_path = str(tmp_path / "tusk" / "tasks.db")
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert not log.exists(), "must be a silent no-op without .claude/bin/"
    captured = capsys.readouterr()
    assert captured.err == ""


def test_noop_when_source_bin_missing(tmp_path, tusk_merge_module, capsys):
    # No bin/ at all — unusual layout, should silently no-op rather than crash.
    (tmp_path / ".claude" / "bin").mkdir(parents=True)
    (tmp_path / "tusk").mkdir()
    db_path = str(tmp_path / "tusk" / "tasks.db")
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert not log.exists()
    captured = capsys.readouterr()
    assert captured.err == ""


def test_disable_via_env_var(tmp_path, tusk_merge_module, capsys, monkeypatch):
    monkeypatch.setenv("TUSK_NO_DEPLOYED_BIN_REFRESH", "1")
    db_path = _source_repo_layout(tmp_path)
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert not log.exists(), "env var should disable the refresh entirely"
    assert (tmp_path / ".claude" / "bin" / "tusk-foo.py").read_text() == "version 1", \
        "deployed file must remain at original content"
    captured = capsys.readouterr()
    assert captured.err == ""


def test_detects_drift_in_tusk_wrapper(tmp_path, tusk_merge_module, capsys):
    # Drift in the bash wrapper itself, not just *.py files.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tusk").write_text("#!/bin/bash\n# v2\n")
    (tmp_path / ".claude" / "bin").mkdir(parents=True)
    (tmp_path / ".claude" / "bin" / "tusk").write_text("#!/bin/bash\n# v1\n")
    (tmp_path / "tusk").mkdir()
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
    db_path = str(tmp_path / "tusk" / "tasks.db")
    tusk_bin, log = _make_fake_tusk_bin(tmp_path)

    tusk_merge_module._maybe_refresh_deployed_bin(db_path, tusk_bin)

    assert log.exists(), "wrapper drift should trigger refresh"
    captured = capsys.readouterr()
    assert "tusk" in captured.err
