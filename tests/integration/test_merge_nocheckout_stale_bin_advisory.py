"""Integration tests for `_maybe_advise_stale_deployed_bin`.

Issue #865: the no-checkout fast-forward path pushes to origin/<default> without
updating primary's working tree, so the auto-refresh helper sees zero drift
(primary's bin/ and primary's .claude/bin/ both stay at primary's pre-merge
content). The advisory helper covers that gap with a one-line stderr hint
naming the recovery command, tailored to primary's working-tree state.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


@pytest.fixture()
def tusk_merge_module():
    bin_dir = os.path.join(REPO_ROOT, "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    spec = importlib.util.spec_from_file_location("tusk_merge_under_test", MERGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, encoding="utf-8")


def _source_repo_layout(tmp_path, *, with_git=True):
    """Build a primary-checkout-shaped layout: bin/, .claude/bin/, tusk/tasks.db.

    When `with_git` is True, also `git init` the layout so `git status --porcelain`
    succeeds inside the helper. Returns the absolute db_path the helper expects.
    """
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tusk-foo.py").write_text("source content\n")
    (tmp_path / ".claude" / "bin").mkdir(parents=True)
    (tmp_path / ".claude" / "bin" / "tusk-foo.py").write_text("source content\n")
    (tmp_path / "tusk").mkdir()
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
    if with_git:
        _run(["git", "init", "-q", "-b", "main"], cwd=tmp_path)
        _run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
        _run(["git", "config", "user.name", "Test"], cwd=tmp_path)
        _run(["git", "add", "."], cwd=tmp_path)
        _run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path)
    return str(tmp_path / "tusk" / "tasks.db")


def test_clean_tree_advisory(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(tmp_path)

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path)

    err = capsys.readouterr().err
    assert "tusk: .claude/bin/ may be stale" in err
    assert "no-checkout merge" in err
    # Issue #877: advisory recommends `tusk sync-main && tusk dev-sync` (one
    # call that fetches + ff-pulls + migrates + refreshes the deployed cache)
    # rather than the manual `git pull && tusk dev-sync` sequence the
    # original wording emitted.
    assert "tusk sync-main && tusk dev-sync" in err
    assert "git pull" not in err, (
        "advisory should not recommend `git pull` — sync-main replaces it"
    )
    assert "Stash or commit" not in err, (
        "clean tree should not get the manual-stash hint"
    )


def test_dirty_tree_advisory_recommends_sync_main(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(tmp_path)
    # Dirty the working tree so `git status --porcelain` reports a change.
    (tmp_path / "CLAUDE.md").write_text("dirty\n")

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path)

    err = capsys.readouterr().err
    assert "tusk: .claude/bin/ may be stale" in err
    # Issue #877: dirty tree no longer asks the operator to stash manually;
    # the advisory points out that sync-main stashes by ref internally.
    assert "tusk sync-main && tusk dev-sync" in err
    assert "stashes local changes by ref" in err
    assert "git pull" not in err, (
        "advisory should not recommend `git pull` — sync-main replaces it"
    )
    assert "Stash or commit" not in err, (
        "manual-stash hint is obsolete — sync-main handles it automatically"
    )


def test_silent_in_consumer_install(tmp_path, tusk_merge_module, capsys):
    # No .claude/bin/ — consumer install layout. Same gate as
    # _maybe_refresh_deployed_bin: silently skip.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tusk-foo.py").write_text("x")
    (tmp_path / "tusk").mkdir()
    (tmp_path / "tusk" / "tasks.db").write_bytes(b"")
    db_path = str(tmp_path / "tusk" / "tasks.db")

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path)

    assert capsys.readouterr().err == ""


def test_silent_when_env_var_disabled(tmp_path, tusk_merge_module, capsys, monkeypatch):
    monkeypatch.setenv("TUSK_NO_DEPLOYED_BIN_REFRESH", "1")
    db_path = _source_repo_layout(tmp_path)

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path)

    assert capsys.readouterr().err == ""


def test_silent_when_not_a_git_repo(tmp_path, tusk_merge_module, capsys):
    # Source-repo layout but no `git init` — `git status --porcelain` returns
    # nonzero, helper can't tell clean vs dirty, stays silent rather than
    # guessing.
    db_path = _source_repo_layout(tmp_path, with_git=False)

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path)

    assert capsys.readouterr().err == ""


def test_refresh_fired_clean_tree_does_not_contradict(tmp_path, tusk_merge_module, capsys):
    # Issue #869: when _maybe_refresh_deployed_bin just announced a successful
    # auto-refresh, the immediately-following advisory must not tell the user
    # ".claude/bin/ may be stale" or to "run tusk dev-sync" as if nothing had
    # been done. Reframe around primary's working tree being behind origin.
    db_path = _source_repo_layout(tmp_path)

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path, refresh_fired=True)

    err = capsys.readouterr().err
    assert "may be stale" not in err, (
        "refresh-fired advisory must not repeat the .claude/bin/-stale framing"
    )
    assert ".claude/bin/" not in err, (
        "refresh-fired advisory should drop the .claude/bin/ noun the first line owned"
    )
    assert "primary's working tree is behind origin" in err
    # Issue #877: refresh-fired clean-tree advisory recommends sync-main too.
    assert "tusk sync-main && tusk dev-sync" in err
    assert "git pull" not in err, (
        "refresh-fired advisory should not recommend `git pull` — sync-main replaces it"
    )
    assert "Stash or commit" not in err, "clean tree should not get the stash-first hint"


def test_refresh_fired_dirty_tree_recommends_sync_main(tmp_path, tusk_merge_module, capsys):
    db_path = _source_repo_layout(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("dirty\n")

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path, refresh_fired=True)

    err = capsys.readouterr().err
    assert "may be stale" not in err
    assert ".claude/bin/" not in err
    assert "primary's working tree is behind origin" in err
    # Issue #877: dirty tree no longer asks the operator to stash manually;
    # the advisory points out that sync-main stashes by ref internally.
    assert "tusk sync-main && tusk dev-sync" in err
    assert "stashes local changes by ref" in err
    assert "git pull" not in err, (
        "refresh-fired advisory should not recommend `git pull` — sync-main replaces it"
    )
    assert "Stash or commit" not in err, (
        "manual-stash hint is obsolete — sync-main handles it automatically"
    )


def test_refresh_fired_still_silent_when_env_var_disabled(
    tmp_path, tusk_merge_module, capsys, monkeypatch,
):
    monkeypatch.setenv("TUSK_NO_DEPLOYED_BIN_REFRESH", "1")
    db_path = _source_repo_layout(tmp_path)

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path, refresh_fired=True)

    assert capsys.readouterr().err == ""
