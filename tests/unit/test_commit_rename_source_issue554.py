"""Regression test for GitHub Issue #554.

`tusk commit` must accept rename source paths (`R` status from
`git diff --cached --name-status -z`) without erroring on `path not found`.

After `git mv old new`, the index holds a deletion for `old` and an
addition for `new` — but `git diff --cached --name-status` reports it
as a single `R<score>\told\tnew` entry, not as separate `D` + `A`
entries. Before the fix, `_get_staged_deletions` skipped `R` entries
entirely, so `old` was treated as a missing-from-disk path with no
staged deletion and the commit aborted with `path not found: 'old'`.

The fix extends `_get_staged_deletions` to include the source path of
`R` entries — it is absent from disk and its deletion is staged at the
index level, so it satisfies the same callers' contract as a pure `D`
entry. Copy (`C`) sources remain excluded: the source file stays in the
working tree, so it is neither absent from disk nor staged for removal.
"""

import importlib.util
import os
import subprocess
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_completed(returncode, stdout="", stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _argv(tmp_path, files):
    config = tmp_path / "config.json"
    config.write_text("{}")
    return [str(tmp_path), str(config), "554", "rename file"] + files


class TestGetStagedDeletionsRenameSource:
    """`_get_staged_deletions` returns rename source paths alongside `D` entries."""

    def test_rename_source_included(self, tmp_path):
        mod = _load_module()

        def fake_run(args, **kwargs):
            if args[:5] == ["git", "diff", "--cached", "--name-status", "-z"]:
                return _make_completed(0, stdout="R100\x00old.txt\x00new.txt\x00")
            return _make_completed(0)

        with patch.object(mod, "run", side_effect=fake_run):
            result = mod._get_staged_deletions(str(tmp_path))

        assert result == {"old.txt"}, (
            f"Rename source 'old.txt' must be in the returned set; got {result}"
        )

    def test_rm_status_treated_like_r(self, tmp_path):
        """`git diff --cached` reports RM (rename + working-tree mod) identically to R.

        The working-tree modification on the destination is invisible to
        --cached, so the index-level view is just `R<score>\told\tnew`.
        This test pins that the parser handles the same shape produced by
        the RM workflow described in Issue #554.
        """
        mod = _load_module()

        def fake_run(args, **kwargs):
            if args[:5] == ["git", "diff", "--cached", "--name-status", "-z"]:
                return _make_completed(0, stdout="R087\x00src/old.swift\x00src/new.swift\x00")
            return _make_completed(0)

        with patch.object(mod, "run", side_effect=fake_run):
            result = mod._get_staged_deletions(str(tmp_path))

        assert result == {"src/old.swift"}

    def test_pure_d_still_included(self, tmp_path):
        """Regression guard: existing D-status acceptance is preserved."""
        mod = _load_module()

        def fake_run(args, **kwargs):
            if args[:5] == ["git", "diff", "--cached", "--name-status", "-z"]:
                return _make_completed(0, stdout="D\x00deleted.txt\x00")
            return _make_completed(0)

        with patch.object(mod, "run", side_effect=fake_run):
            result = mod._get_staged_deletions(str(tmp_path))

        assert result == {"deleted.txt"}

    def test_copy_source_excluded(self, tmp_path):
        """C-status: source remains in the working tree — must NOT be in the set."""
        mod = _load_module()

        def fake_run(args, **kwargs):
            if args[:5] == ["git", "diff", "--cached", "--name-status", "-z"]:
                return _make_completed(0, stdout="C100\x00source.txt\x00copy.txt\x00")
            return _make_completed(0)

        with patch.object(mod, "run", side_effect=fake_run):
            result = mod._get_staged_deletions(str(tmp_path))

        assert result == set(), (
            f"Copy source must not be returned (it stays in the working tree); got {result}"
        )

    def test_mixed_r_d_c_and_m(self, tmp_path):
        """All four index states in one diff: R-source in, D in, C-source out, M out."""
        mod = _load_module()

        # NUL-separated stream:
        #   R100 \0 old.txt \0 new.txt \0  (rename: source goes IN)
        #   D    \0 gone.txt \0            (pure deletion: goes IN)
        #   C075 \0 source.py \0 copy.py \0 (copy: source stays OUT)
        #   M    \0 changed.txt \0         (modification: not a deletion at all)
        stream = (
            "R100\x00old.txt\x00new.txt\x00"
            "D\x00gone.txt\x00"
            "C075\x00source.py\x00copy.py\x00"
            "M\x00changed.txt\x00"
        )

        def fake_run(args, **kwargs):
            if args[:5] == ["git", "diff", "--cached", "--name-status", "-z"]:
                return _make_completed(0, stdout=stream)
            return _make_completed(0)

        with patch.object(mod, "run", side_effect=fake_run):
            result = mod._get_staged_deletions(str(tmp_path))

        assert result == {"old.txt", "gone.txt"}


class TestCommitFlowAcceptsRenameSource:
    """End-to-end: `tusk commit old new` after `git mv old new` does not error.

    Validates that the rename source path passes the missing-files check
    (Step 0 in tusk-commit.py) and is excluded from the `git add` call
    (Step 3) so it doesn't trigger a pathspec error.
    """

    def test_rename_source_path_passes_validation_and_is_not_added(self, tmp_path, capsys):
        mod = _load_module()

        # Destination exists on disk (as it would after `git mv` + edit).
        new_file = tmp_path / "new.txt"
        new_file.write_text("modified contents")

        # Source does NOT exist on disk (git mv removed it).
        # User passes BOTH paths to tusk commit.
        argv = _argv(tmp_path, ["old.txt", "new.txt"])

        captured_add_args = []

        def fake_run(args, **kwargs):
            # Pre-flight tracked-status check on the missing source.
            if args[:3] == ["git", "ls-files", "--"]:
                # `old.txt` is no longer in the index (git mv removed it
                # from the source slot and added it to the dest slot).
                return _make_completed(0, stdout="")
            # Index inspection for staged absent paths — this is the
            # call the fix targets. Report the rename so the source is
            # accepted as a legitimately-staged absent path.
            if args[:5] == ["git", "diff", "--cached", "--name-status", "-z"]:
                return _make_completed(0, stdout="R100\x00old.txt\x00new.txt\x00")
            # Step 2.5 unstaged-deletions scan — nothing extra.
            if args[:3] == ["git", "ls-files", "--deleted"]:
                return _make_completed(0, stdout="")
            if args[:2] == ["git", "add"]:
                captured_add_args.append(list(args))
                return _make_completed(0)
            if args[:2] == ["git", "rev-parse"]:
                return _make_completed(0, stdout="aaa111\n")
            if args[:2] == ["git", "commit"]:
                return _make_completed(0, stdout="[main bbb222] rename file")
            return _make_completed(0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("os.getcwd", return_value=str(tmp_path)):
            rc = mod.main(argv)

        assert rc == 0, "commit should not error on the rename source path"
        captured = capsys.readouterr()
        assert "path not found" not in (captured.out + captured.err), (
            "the rename source must not trigger Step 0's path-not-found error"
        )
        assert len(captured_add_args) == 1, "exactly one git add call expected"
        staged = captured_add_args[0]
        # The destination is added; the source must NOT be passed to git add
        # (it has no working-tree file, so `git add old.txt` would itself fail).
        assert "new.txt" in staged
        assert "old.txt" not in staged, (
            f"rename source must be excluded from 'git add' (it lives in the index "
            f"already, not on disk); saw: {staged}"
        )
