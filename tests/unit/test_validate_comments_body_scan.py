"""Unit tests for the body-scan helpers in ``cmd_validate_comments``.

Pins the behavior of ``_extract_paths`` and ``_path_in_diff`` (issue #912):
general (null ``file_path``) review comments are scanned for file-path-shaped
tokens, and a comment whose cited paths are entirely absent from
``git diff --name-only`` is dismissed under the same fabrication-guard
rationale as non-null ``file_path`` comments (issue #783).
"""

import importlib.util
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN_DIR = os.path.join(REPO_ROOT, "bin")


def _load_review_module():
    sys.path.insert(0, BIN_DIR)
    spec = importlib.util.spec_from_file_location(
        "tusk_review_under_test", os.path.join(BIN_DIR, "tusk-review.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review = _load_review_module()


class TestExtractPaths:
    def test_empty_or_none_returns_empty(self):
        assert review._extract_paths(None) == []
        assert review._extract_paths("") == []
        assert review._extract_paths("   \n\t  ") == []

    def test_extracts_multi_segment_path_with_extension(self):
        body = "the change to apps/foo/nonexistent.py looks wrong"
        assert review._extract_paths(body) == ["apps/foo/nonexistent.py"]

    def test_extracts_bare_basename_with_extension(self):
        body = "friendlysky.py and east_austin_comedy.py are bundled"
        assert review._extract_paths(body) == [
            "friendlysky.py",
            "east_austin_comedy.py",
        ]

    def test_extracts_multiple_distinct_paths(self):
        body = (
            "bin/tusk-review.py calls into tests/integration/test_x.py "
            "which depends on CHANGELOG.md"
        )
        assert review._extract_paths(body) == [
            "bin/tusk-review.py",
            "tests/integration/test_x.py",
            "CHANGELOG.md",
        ]

    def test_deduplicates_preserving_first_seen_order(self):
        body = "apps/foo.py and again apps/foo.py and once more apps/foo.py"
        assert review._extract_paths(body) == ["apps/foo.py"]

    def test_strips_trailing_punctuation(self):
        body = "see bin/tusk-review.py, also tests/test_x.py."
        assert review._extract_paths(body) == [
            "bin/tusk-review.py",
            "tests/test_x.py",
        ]

    def test_rejects_word_with_dotted_abbreviation(self):
        body = "i.e. this is fine; e.g. nothing matches here"
        assert review._extract_paths(body) == []

    def test_rejects_version_strings_and_decimals(self):
        body = "python 3.12 with coverage 98.4 and version 1.2.3"
        assert review._extract_paths(body) == []

    def test_rejects_token_without_known_extension(self):
        body = "see apps/foo/bar.unknown for context"
        assert review._extract_paths(body) == []

    def test_handles_extension_case_insensitively(self):
        body = "see APP/Main.PY and README.MD"
        # The capture preserves the original casing.
        assert review._extract_paths(body) == ["APP/Main.PY", "README.MD"]

    def test_extracts_path_inside_backticks(self):
        body = "the helper `bin/tusk-review.py` was updated"
        assert review._extract_paths(body) == ["bin/tusk-review.py"]

    def test_does_not_match_inside_url_host(self):
        # gioe/tusk in a URL host is not a project file path; only the
        # path portion after the host counts. The current heuristic
        # would match "issues/912" only if that segment had a known
        # extension — which it doesn't, so the result is empty.
        body = "see https://github.com/gioe/tusk/issues/912 for context"
        assert review._extract_paths(body) == []


class TestPathInDiff:
    def test_multi_segment_full_path_match(self):
        diff_files = {"apps/foo/bar.py", "tests/test_x.py"}
        assert review._path_in_diff("apps/foo/bar.py", diff_files) is True

    def test_multi_segment_no_basename_fallback(self):
        # Confabulated apps/foo/bar.py must NOT be rescued by a
        # same-basename file at a different location.
        diff_files = {"src/bar.py"}
        assert review._path_in_diff("apps/foo/bar.py", diff_files) is False

    def test_bare_basename_matches_any_diff_file_basename(self):
        diff_files = {"apps/foo/bar.py", "tests/test_x.py"}
        assert review._path_in_diff("bar.py", diff_files) is True

    def test_bare_basename_with_no_match_returns_false(self):
        diff_files = {"apps/foo/bar.py"}
        assert review._path_in_diff("nonexistent.py", diff_files) is False

    def test_empty_diff_files_rejects_everything(self):
        assert review._path_in_diff("bar.py", set()) is False
        assert review._path_in_diff("apps/foo/bar.py", set()) is False
