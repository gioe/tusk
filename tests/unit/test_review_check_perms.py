"""Unit tests for tusk-review-check-perms.py.

Exit paths covered:
- on-disk settings.json with all required permissions → OK, exit 0
- on-disk settings.json missing some entries → MISSING: <entries>, exit 1
- no on-disk settings.json and `git show HEAD:.claude/settings.json` returns non-zero → MISSING: not found, exit 1
- on-disk settings.json absent but HEAD copy is valid → OK, exit 0
- on-disk settings.json is not valid JSON → MISSING: not valid JSON, exit 1
- permissions or permissions.allow is not of the expected shape → MISSING: …, exit 1
"""

import importlib.util
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
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


def test_permissions_not_a_dict(tmp_path):
    """permissions is a string, not an object — should not raise AttributeError."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": "whatever"}))
    rc, out = _run_check(repo)
    assert rc == 1
    assert out.startswith("MISSING: ")
    assert "permissions" in out


def test_permissions_allow_not_a_list(tmp_path):
    """permissions.allow is a dict instead of a list — should not raise TypeError."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": {"a": 1}}}))
    rc, out = _run_check(repo)
    assert rc == 1
    assert out.startswith("MISSING: ")
    assert "allow" in out


# ── CWD-vs-repo_root settings-resolution mismatch (issue #1091) ─────────────


def _run_check_cwd(repo_root: str, cwd: str) -> tuple[int, str, str]:
    """Run check() with an explicit CWD, capturing stdout and stderr."""
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.check(repo_root, cwd=cwd)
    return rc, out.getvalue().strip(), err.getvalue().strip()


def _make_two_root(tmp_path, worktree_settings: str | None):
    """Build a primary checkout (full perms, real .git) and a linked-worktree
    layout (`.git` as a bogus gitdir *file*, optional .claude/settings.json) with
    a nested `apps/scraper` CWD — mirroring the issue #1091 incident.

    Returns ``(primary_root, worktree_root, scraper_cwd)``.
    """
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)  # primary checkout: .git is a directory
    (primary / "tusk").mkdir()
    (primary / "tusk" / "tasks.db").write_bytes(b"")
    (primary / ".claude").mkdir()
    (primary / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ALL_REQUIRED}})
    )

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # Linked worktrees carry a `.git` *file* pointing at a gitdir; a bogus target
    # is enough for _find_git_root (existence check) and makes `git show` fail,
    # which models a worktree root with no usable committed settings.
    (worktree / ".git").write_text("gitdir: /nonexistent/.git/worktrees/wt\n")
    if worktree_settings is not None:
        (worktree / ".claude").mkdir()
        (worktree / ".claude" / "settings.json").write_text(worktree_settings)

    scraper = worktree / "apps" / "scraper"
    scraper.mkdir(parents=True)
    return str(primary), str(worktree), str(scraper)


def test_mismatch_when_worktree_settings_absent(tmp_path):
    """Worktree-subdir CWD with NO inheritable settings → MISMATCH, exit 2."""
    primary, worktree, scraper = _make_two_root(tmp_path, worktree_settings=None)
    rc, out, _err = _run_check_cwd(primary, scraper)
    assert rc == 2
    assert out.startswith("MISMATCH: ")
    # Names the file a subagent would actually resolve, and the validated one.
    assert os.path.join(worktree, ".claude", "settings.json") in out
    assert os.path.join(primary, ".claude", "settings.json") in out


def test_mismatch_when_worktree_settings_insufficient(tmp_path):
    """Worktree-subdir CWD whose settings lack a required perm → MISMATCH, exit 2."""
    partial = json.dumps({"permissions": {"allow": ALL_REQUIRED[:2]}})
    primary, worktree, scraper = _make_two_root(tmp_path, worktree_settings=partial)
    rc, out, _err = _run_check_cwd(primary, scraper)
    assert rc == 2
    assert out.startswith("MISMATCH: ")
    # The lacking permissions are named in the mismatch explanation.
    for missing in ALL_REQUIRED[2:]:
        assert missing in out


def test_ok_when_worktree_settings_grant_perms(tmp_path):
    """Worktree-subdir CWD whose settings DO grant the perms → OK, exit 0,
    with a stderr note that the CWD project root diverged but is also covered."""
    full = json.dumps({"permissions": {"allow": ALL_REQUIRED}})
    primary, worktree, scraper = _make_two_root(tmp_path, worktree_settings=full)
    rc, out, err = _run_check_cwd(primary, scraper)
    assert rc == 0
    assert out == "OK"
    assert "CWD project root differs" in err
    assert os.path.join(worktree, ".claude", "settings.json") in err


def test_in_root_cwd_subdir_unchanged(tmp_path):
    """CWD inside the db-derived repo_root (a subdir) → no mismatch, OK, exit 0,
    and stderr carries no divergence note."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": ALL_REQUIRED}}))
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "bin"
    subdir.mkdir()
    rc, out, err = _run_check_cwd(repo, str(subdir))
    assert rc == 0
    assert out == "OK"
    assert "differs" not in err
    assert os.path.join(repo, ".claude", "settings.json") in err


def test_in_root_cwd_is_repo_root_unchanged(tmp_path):
    """CWD == db-derived repo_root → no mismatch, OK, exit 0."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": ALL_REQUIRED}}))
    (tmp_path / ".git").mkdir()
    rc, out, err = _run_check_cwd(repo, repo)
    assert rc == 0
    assert out == "OK"
    assert "differs" not in err


def test_mismatch_detection_skipped_when_repo_root_not_a_checkout(tmp_path):
    """When the db-derived repo_root has no .git (e.g. TUSK_DB pins the DB outside
    any repo), mismatch detection is skipped — legacy OK behavior is preserved
    even though the CWD resolves to a different project root."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": ALL_REQUIRED}}))
    # No .git at repo → guard skips the mismatch branch. Point CWD at an
    # unrelated worktree that DOES have its own .git to prove the branch is gated
    # on repo_root being a checkout, not on the CWD.
    other = tmp_path / "other-root"
    (other / ".git").mkdir(parents=True)
    rc, out, _err = _run_check_cwd(repo, str(other))
    assert rc == 0
    assert out == "OK"


def test_legacy_check_without_cwd_skips_mismatch(tmp_path):
    """check() with no cwd argument never runs mismatch detection (backward
    compatible with the existing call sites and unit tests)."""
    repo = _make_repo(tmp_path, json.dumps({"permissions": {"allow": ALL_REQUIRED}}))
    (tmp_path / ".git").mkdir()
    rc, out = _run_check(repo)
    assert rc == 0
    assert out == "OK"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
