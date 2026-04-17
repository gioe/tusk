"""Unit tests for tusk-review-check-perms.py.

Covers the three exit paths:
- on-disk settings.json with all required permissions → OK, exit 0
- on-disk settings.json missing some entries → MISSING: <entries>, exit 1
- no on-disk settings.json and `git show HEAD:.claude/settings.json` returns non-zero → MISSING: not found, exit 1
- on-disk settings.json absent but HEAD copy is valid → OK, exit 0
"""

import importlib.util
import json
import os
import subprocess
from contextlib import redirect_stdout
from io import StringIO

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_review_check_perms",
    os.path.join(BIN, "tusk-review-check-perms.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


ALL_REQUIRED = list(mod.REQUIRED_PERMISSIONS)


def _make_repo(tmp_path, settings_content: str | None):
    """Create a fake repo at tmp_path; optionally write .claude/settings.json."""
    (tmp_path / "tusk").mkdir()
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
    if settings_content is not None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(settings_content)
    return str(tmp_path)


def _run_check(repo_root: str) -> tuple[int, str]:
    buf = StringIO()
    with redirect_stdout(buf):
        rc = mod.check(repo_root)
    return rc, buf.getvalue().strip()


def test_ok_when_all_permissions_present(tmp_path):
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": ALL_REQUIRED}}))
    rc, out = _run_check(repo)
    assert rc == 0
    assert out == "OK"


def test_missing_lists_only_missing_entries(tmp_path):
    partial = ALL_REQUIRED[:2]
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": partial}}))
    rc, out = _run_check(repo)
    assert rc == 1
    assert out.startswith("MISSING: ")
    listed = out[len("MISSING: "):].split(", ")
    assert set(listed) == set(ALL_REQUIRED[2:])


def test_missing_when_no_settings_and_no_head(tmp_path):
    """No on-disk file and not a git repo → git show fails → MISSING: not found."""
    repo = _make_repo(tmp_path, settings_content=None)
    rc, out = _run_check(repo)
    assert rc == 1
    assert "not found on disk or in HEAD" in out


def test_head_fallback_ok_when_on_disk_missing(tmp_path):
    """Delete on-disk settings.json but keep a valid copy in HEAD; expect OK via git show."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": ALL_REQUIRED}}))

    def _git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", ".claude/settings.json")
    _git("commit", "-q", "-m", "initial")

    os.remove(os.path.join(repo, ".claude", "settings.json"))

    rc, out = _run_check(repo)
    assert rc == 0
    assert out == "OK"


def test_invalid_json_on_disk(tmp_path):
    repo = _make_repo(tmp_path, "{not valid json")
    rc, out = _run_check(repo)
    assert rc == 1
    assert "not valid JSON" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
