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


# Issue #880: when tusk_bin is provided and TUSK_NO_AUTO_SYNC_MAIN is unset,
# the advisory turns into an auto-action — invoke `tusk sync-main` against the
# primary checkout, emit a status line on success, fall back to the existing
# advisory on failure, and re-run the deployed-bin refresh so any newly-pulled
# bin/tusk-*.py content propagates to .claude/bin/ before the next session.


def _fake_completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["tusk", "sync-main"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_auto_sync_success_emits_status_line(
    tmp_path, tusk_merge_module, capsys, monkeypatch,
):
    db_path = _source_repo_layout(tmp_path)
    sync_calls = []

    def fake_run_sync_main(tusk_bin, primary_root):
        sync_calls.append((tusk_bin, str(primary_root)))
        return _fake_completed(0, stdout='{"default_branch": "main", "success": true}')

    refresh_calls = []

    def fake_refresh(db_path_arg, tusk_bin_arg):
        refresh_calls.append((db_path_arg, tusk_bin_arg))
        return False

    monkeypatch.setattr(tusk_merge_module, "_run_sync_main", fake_run_sync_main)
    monkeypatch.setattr(tusk_merge_module, "_maybe_refresh_deployed_bin", fake_refresh)

    tusk_merge_module._maybe_advise_stale_deployed_bin(
        db_path, tusk_bin="/fake/tusk",
    )

    err = capsys.readouterr().err
    assert "auto-synced primary to origin/main via tusk sync-main" in err
    assert ".claude/bin/ may be stale" not in err, (
        "auto-sync success path must replace, not append to, the advisory wording"
    )
    assert "Run `tusk sync-main && tusk dev-sync`" not in err, (
        "auto-sync success path must not still instruct the operator to run sync-main"
    )
    assert sync_calls == [("/fake/tusk", str(tmp_path))], (
        "tusk sync-main should be invoked exactly once from primary_root"
    )


def test_auto_sync_failure_falls_back_to_advisory(
    tmp_path, tusk_merge_module, capsys, monkeypatch,
):
    db_path = _source_repo_layout(tmp_path)

    def fake_run_sync_main(tusk_bin, primary_root):
        return _fake_completed(1, stderr="fatal: refusing to merge unrelated histories\n")

    refresh_calls = []
    monkeypatch.setattr(tusk_merge_module, "_run_sync_main", fake_run_sync_main)
    monkeypatch.setattr(
        tusk_merge_module, "_maybe_refresh_deployed_bin",
        lambda *a, **kw: refresh_calls.append(a) or False,
    )

    tusk_merge_module._maybe_advise_stale_deployed_bin(
        db_path, tusk_bin="/fake/tusk",
    )

    err = capsys.readouterr().err
    assert "auto-sync failed (tusk sync-main exit 1)" in err, (
        "failure prefix must name the sync-main exit code"
    )
    assert "fall back to manual recovery below" in err
    # The existing four-variant advisory wording must follow the failure prefix —
    # this is a clean tree so it's the "may be stale ... Run tusk sync-main && tusk dev-sync"
    # variant from issue #877.
    assert ".claude/bin/ may be stale" in err
    assert "Run `tusk sync-main && tusk dev-sync`" in err
    assert refresh_calls == [], (
        "the deployed-bin refresh should not chain after a failed auto-sync"
    )


def test_tusk_no_auto_sync_main_env_var_preserves_existing_advisory(
    tmp_path, tusk_merge_module, capsys, monkeypatch,
):
    monkeypatch.setenv("TUSK_NO_AUTO_SYNC_MAIN", "1")
    db_path = _source_repo_layout(tmp_path)

    sync_calls = []
    monkeypatch.setattr(
        tusk_merge_module, "_run_sync_main",
        lambda *a, **kw: sync_calls.append(a) or _fake_completed(0),
    )

    tusk_merge_module._maybe_advise_stale_deployed_bin(
        db_path, tusk_bin="/fake/tusk",
    )

    err = capsys.readouterr().err
    # Auto-action is opted out — operator sees the same advisory issue #877 emits.
    assert ".claude/bin/ may be stale" in err
    assert "Run `tusk sync-main && tusk dev-sync`" in err
    assert "auto-synced primary" not in err, (
        "TUSK_NO_AUTO_SYNC_MAIN=1 must suppress the auto-action status line"
    )
    assert "auto-sync failed" not in err
    assert sync_calls == [], "tusk sync-main must NOT run when TUSK_NO_AUTO_SYNC_MAIN=1"


def test_auto_sync_success_chains_deployed_bin_refresh(
    tmp_path, tusk_merge_module, capsys, monkeypatch,
):
    """After a successful auto-sync, re-run _maybe_refresh_deployed_bin so any
    bin/tusk-*.py drift introduced by sync-main propagates to .claude/bin/.
    """
    db_path = _source_repo_layout(tmp_path)

    monkeypatch.setattr(
        tusk_merge_module, "_run_sync_main",
        lambda *a, **kw: _fake_completed(0, stdout='{"default_branch": "main"}'),
    )

    refresh_calls = []

    def fake_refresh(db_path_arg, tusk_bin_arg):
        refresh_calls.append((db_path_arg, tusk_bin_arg))
        return False

    monkeypatch.setattr(tusk_merge_module, "_maybe_refresh_deployed_bin", fake_refresh)

    tusk_merge_module._maybe_advise_stale_deployed_bin(
        db_path, tusk_bin="/fake/tusk",
    )

    assert refresh_calls == [(db_path, "/fake/tusk")], (
        "deployed-bin refresh must chain after a successful auto-sync, "
        "with the same db_path and tusk_bin"
    )


# Issue #908: when sync-main exits non-zero from inside tusk merge, the
# fallback advisory should route by the specific failure mode (stash refused,
# ff refused, pop failed, migrate failed, fetch failed) instead of
# re-recommending `tusk sync-main` verbatim — the command that just exited 1.
# Unmerged-conflict detection on primary takes priority because it is the most
# common root cause (the original incident on this issue was a pre-session
# UU CLAUDE.md preventing the internal stash push).


def _stderr_routed_advisory(tmp_path, tusk_merge_module, monkeypatch, capsys, stderr):
    """Drive _maybe_advise_stale_deployed_bin with a fake sync-main failure
    carrying the given stderr and return the captured stderr text."""
    db_path = _source_repo_layout(tmp_path)
    monkeypatch.setattr(
        tusk_merge_module, "_run_sync_main",
        lambda *a, **kw: _fake_completed(
            1, stdout='{"default_branch": "main", "success": false}',
            stderr=stderr,
        ),
    )
    monkeypatch.setattr(
        tusk_merge_module, "_maybe_refresh_deployed_bin", lambda *a, **kw: False,
    )
    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path, tusk_bin="/fake/tusk")
    return capsys.readouterr().err


def test_auto_sync_fetch_failure_routes_to_fetch_advisory(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    err = _stderr_routed_advisory(
        tmp_path, tusk_merge_module, monkeypatch, capsys,
        stderr="Error: git fetch origin main failed: network unreachable",
    )
    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "`git fetch origin main` failed inside sync-main" in err
    assert "Check network/auth" in err
    assert ".claude/bin/ may be stale" not in err, (
        "routed advisory must not append the indeterminate four-variant fallback"
    )


def test_auto_sync_stash_failure_routes_to_stash_advisory(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    err = _stderr_routed_advisory(
        tmp_path, tusk_merge_module, monkeypatch, capsys,
        stderr="Error: git stash push failed: could not write index",
    )
    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "`git stash push` refused inside sync-main" in err
    assert "unmerged paths or a corrupt index" in err
    assert ".claude/bin/ may be stale" not in err


def test_auto_sync_ff_failure_routes_to_ff_advisory(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    err = _stderr_routed_advisory(
        tmp_path, tusk_merge_module, monkeypatch, capsys,
        stderr=(
            "Error: git merge --ff-only origin/main failed: "
            "fatal: Not possible to fast-forward"
        ),
    )
    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "`git merge --ff-only origin/main` refused" in err
    assert "local has diverged from origin" in err
    assert "git pull --rebase origin main" in err
    assert ".claude/bin/ may be stale" not in err


def test_auto_sync_pop_failure_routes_to_pop_advisory(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    err = _stderr_routed_advisory(
        tmp_path, tusk_merge_module, monkeypatch, capsys,
        stderr="Error: git stash pop stash@{0} failed: conflict in CLAUDE.md",
    )
    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "`git stash pop` failed after the ff-pull" in err
    assert "remain stashed" in err
    assert ".claude/bin/ may be stale" not in err


def test_auto_sync_migrate_failure_routes_to_migrate_advisory(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    err = _stderr_routed_advisory(
        tmp_path, tusk_merge_module, monkeypatch, capsys,
        stderr="Error: tusk migrate failed: schema version mismatch",
    )
    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "`tusk migrate` failed after a successful sync" in err
    assert "pending schema migrations were not applied" in err
    assert ".claude/bin/ may be stale" not in err


def test_auto_sync_failure_with_unmerged_paths_takes_priority_over_stderr(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    """Pre-existing UU/AA/DD rows on primary trump any sync-main stderr signature.

    This is the actual root-cause scenario from issue #908: primary had a
    pre-session UU CLAUDE.md, so sync-main's stash push failed. The advisory
    should name the unmerged path instead of routing to the (correct but less
    actionable) `git stash push refused` message.
    """
    db_path = _source_repo_layout(tmp_path)

    # Create a real UU unmerged conflict on CLAUDE.md in the tmp repo.
    _run(["git", "checkout", "-q", "-b", "feat-conflict"], cwd=tmp_path)
    (tmp_path / "CLAUDE.md").write_text("from feat-conflict\n")
    _run(["git", "add", "CLAUDE.md"], cwd=tmp_path)
    _run(["git", "commit", "-q", "-m", "feat-conflict edit"], cwd=tmp_path)
    _run(["git", "checkout", "-q", "main"], cwd=tmp_path)
    (tmp_path / "CLAUDE.md").write_text("from main\n")
    _run(["git", "add", "CLAUDE.md"], cwd=tmp_path)
    _run(["git", "commit", "-q", "-m", "main edit"], cwd=tmp_path)
    # `git merge feat-conflict` exits non-zero on conflict — don't use _run
    # (which raises) here.
    subprocess.run(
        ["git", "merge", "feat-conflict"], cwd=str(tmp_path),
        capture_output=True, encoding="utf-8",
    )

    monkeypatch.setattr(
        tusk_merge_module, "_run_sync_main",
        lambda *a, **kw: _fake_completed(
            1, stdout='{"default_branch": "main", "success": false}',
            stderr="Error: git stash push failed: could not write index",
        ),
    )
    monkeypatch.setattr(
        tusk_merge_module, "_maybe_refresh_deployed_bin", lambda *a, **kw: False,
    )

    tusk_merge_module._maybe_advise_stale_deployed_bin(db_path, tusk_bin="/fake/tusk")
    err = capsys.readouterr().err

    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "primary has unmerged paths" in err
    assert "CLAUDE.md" in err
    # Unmerged-path branch wins over the stderr-based stash classification.
    assert "`git stash push` refused" not in err, (
        "unmerged-path detection must take priority over stderr-signature routing"
    )
    assert ".claude/bin/ may be stale" not in err


def test_auto_sync_indeterminate_failure_falls_back_to_four_variant(
    tmp_path, tusk_merge_module, monkeypatch, capsys,
):
    """When stderr matches no known signature AND primary is clean, fall back
    to the issue #877 four-variant advisory verbatim. This is the
    backwards-compatible escape hatch that preserves the original wording for
    failure modes the classifier doesn't recognize yet."""
    err = _stderr_routed_advisory(
        tmp_path, tusk_merge_module, monkeypatch, capsys,
        stderr="fatal: some novel error sync-main has not seen before\n",
    )
    assert "auto-sync failed (tusk sync-main exit 1)" in err
    assert "fall back to manual recovery below" in err
    # The four-variant clean-tree wording must follow the prefix line.
    assert ".claude/bin/ may be stale" in err
    assert "Run `tusk sync-main && tusk dev-sync`" in err
