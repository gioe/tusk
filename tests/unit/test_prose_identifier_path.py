"""Unit coverage for is_prose_identifier_path's dot-segment rule (issue #1093).

The scope-derivation consumers (task-insert / task-update) filter
auto-derived path candidates through is_prose_identifier_path. The original
rule only inspected the first path segment for a dot prefix, so a symmetric
runtime-dir concatenation such as ``node_modules/.venv`` (extracted from
prose like "manual deletion of .venv/node_modules") slipped through and
landed as a bogus auto_derived task_scope row. The rule now flags a
dot-prefixed segment ANYWHERE in a non-existent path.
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


def _is_prose(path, exists=False):
    """Evaluate the rule with path existence stubbed out (no real repo)."""
    import unittest.mock as mock

    with mock.patch.object(mod, "path_exists_in_repo", return_value=exists):
        return mod.is_prose_identifier_path(path, "/repo")


def test_dot_segment_in_first_position_is_prose():
    assert _is_prose(".venv/node_modules") is True


def test_dot_segment_in_later_position_is_prose():
    # The issue #1093 regression: first segment is a plain name, the
    # dot-prefixed segment is second, so the first-segment-only rule missed it.
    assert _is_prose("node_modules/.venv") is True


def test_dot_segment_mid_path_is_prose():
    assert _is_prose("foo/.git/bar") is True


def test_dotted_first_segment_is_prose():
    assert _is_prose("console.error/console.log") is True


def test_numeric_version_segment_is_prose():
    assert _is_prose("Mozilla/5.0") is True


def test_real_source_path_is_not_prose():
    assert _is_prose("apps/web/app/api/health/route.test.ts") is False


def test_bracketed_route_path_is_not_prose():
    assert _is_prose("apps/web/app/admin/clubs/[id]/page.tsx") is False


def test_first_segment_rooted_create_is_not_prose():
    assert _is_prose("tests/no/such_scope_suffix_file.py") is False


def test_single_segment_token_is_not_prose():
    # No slash → never a slash-joined identifier.
    assert _is_prose("VERSION") is False


def test_existing_dot_path_is_not_prose():
    # A real, tracked path with a dot-dir is kept (existence short-circuits).
    assert _is_prose(".github/workflows/ci.yml", exists=True) is False
