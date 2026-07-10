"""Unit coverage for is_trackable_scope_pattern (issue #1116).

task-insert / task-update auto-derive ``task_scope`` rows from a task's
summary, description, and criteria. Before this guard the scope-table
derivation diverged from the worktree cone derivation (issue #1044): a
consumer-repo path quoted in a GitHub issue body (e.g.
``apps/web/ui/pages/entity/podcast/index.test.tsx``, nonexistent in the tusk
repo) was dropped by the cone but kept as a phantom ``auto_derived`` scope
row, which then produced a spurious ``missing_scope_path``
context_health_warning. ``is_trackable_scope_pattern`` applies the same
tracked-path validation in both consumers so the two no longer diverge.
"""

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_git_helpers",
    os.path.join(BIN, "tusk-git-helpers.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# Simulated tracked repo entries (top-level dirs, top-level files, and one
# nested tracked file). is_trackable_scope_pattern probes path_exists_in_repo
# for the full path first, then for its top-level segment.
_TRACKED = {
    "bin",
    "tests",
    "docs",
    "CLAUDE.md",
    "VERSION",
    "bin/tusk-task-insert.py",
}


def _is_trackable(pattern):
    import unittest.mock as mock

    def fake_exists(repo_root, path):
        return path in _TRACKED

    with mock.patch.object(mod, "path_exists_in_repo", side_effect=fake_exists):
        return mod.is_trackable_scope_pattern("/repo", pattern)


def test_foreign_multi_segment_path_is_dropped():
    # The issue #1116 regression: a consumer-repo path whose top-level segment
    # (apps) is not tracked in this repo must not become a scope row.
    assert _is_trackable("apps/web/ui/pages/entity/podcast/index.test.tsx") is False


def test_foreign_short_path_is_dropped():
    assert _is_trackable("apps/web/does/not/exist.test.tsx") is False


def test_tracked_file_is_kept():
    assert _is_trackable("bin/tusk-task-insert.py") is True


def test_new_file_under_tracked_dir_is_kept():
    # File does not exist yet, but its top-level segment (tests) is tracked —
    # a plausible new file the task creates under an existing tree.
    assert _is_trackable("tests/unit/test_brand_new_thing.py") is True


def test_description_only_new_file_under_tracked_dir_is_dropped():
    import unittest.mock as mock

    with mock.patch.object(
        mod, "path_exists_in_repo", side_effect=lambda repo_root, path: path in _TRACKED
    ):
        assert mod.is_trackable_scope_pattern(
            "/repo",
            ".github/workflows/ios.yml",
            allow_new_under_tracked=False,
        ) is False


def test_intent_backed_new_file_under_tracked_dir_is_kept():
    import unittest.mock as mock

    tracked = {*_TRACKED, ".github"}
    with mock.patch.object(
        mod, "path_exists_in_repo", side_effect=lambda repo_root, path: path in tracked
    ):
        assert mod.is_trackable_scope_pattern(
            "/repo",
            ".github/workflows/new-local.yml",
            allow_new_under_tracked=True,
        ) is True


def test_new_nested_dir_under_tracked_top_is_kept():
    # bin is tracked even though bin/sub does not exist yet.
    assert _is_trackable("bin/sub/newscript.py") is True


def test_single_segment_new_top_level_file_is_kept():
    # No parent tree to anchor against; explicitly named, not a foreign path.
    assert _is_trackable("NEWDOC.md") is True


def test_single_segment_tracked_top_level_file_is_kept():
    assert _is_trackable("VERSION") is True


def test_trailing_slash_dir_under_untracked_top_is_dropped():
    assert _is_trackable("apps/web/") is False


def test_trailing_slash_dir_under_tracked_top_is_kept():
    assert _is_trackable("docs/newsubdir/") is True


def test_glob_pattern_under_untracked_top_is_dropped():
    # A resolved trailing-slash dir reference becomes ``<dir>/**``; validate the
    # directory portion, not the glob.
    assert _is_trackable("apps/web/**") is False


def test_glob_pattern_under_tracked_top_is_kept():
    assert _is_trackable("bin/**") is True


def test_empty_pattern_is_kept():
    assert _is_trackable("") is True


def test_no_repo_root_keeps_pattern():
    # Without a repo root the path cannot be validated; keep it (no validation),
    # matching the cone validator's behavior when git ls-tree is unavailable.
    assert mod.is_trackable_scope_pattern(None, "apps/web/foreign.tsx") is True
