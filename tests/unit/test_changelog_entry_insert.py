"""Unit tests for _insert_changelog_entry (issue #954).

The VERSION/CHANGELOG rebase-conflict resolver used to write the reassigned
version block followed by the entire upstream changelog, which placed the new
block ABOVE upstream's '# Changelog' title and stacked a duplicate title on every
rebase-merge. _insert_changelog_entry replaces that write with an in-place
insertion below the '## [Unreleased]' marker, mirroring tusk-changelog-add.py.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    tusk_loader_mock = MagicMock()
    tusk_loader_mock.load.return_value = MagicMock()
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


tusk_merge = _load_module()

_PREAMBLE = (
    "# Changelog\n\n"
    "All notable changes to tusk are documented in this file.\n\n"
    "Format based on [Keep a Changelog](https://keepachangelog.com/), "
    "adapted for integer versioning.\n\n"
)


def _title_line_index(content: str) -> int:
    lines = content.splitlines()
    return next(i for i, line in enumerate(lines) if line == "# Changelog")


def test_insert_below_unreleased_keeps_single_title():
    upstream = _PREAMBLE + "## [Unreleased]\n\n## [1044] - 2026-05-28\n\n- [TASK-528] prior\n"
    entry = "## [1045] - 2026-05-29\n\n- [TASK-530] fix resolver\n"

    result = tusk_merge._insert_changelog_entry(upstream, entry)

    # Exactly one title, and nothing was prepended above it.
    assert result.count("# Changelog") == 1
    title_idx = _title_line_index(result)
    above_title = "\n".join(result.splitlines()[:title_idx])
    assert "## [" not in above_title

    # New entry lands above the prior entry (topmost versioned section).
    assert result.index("## [1045]") < result.index("## [1044]")
    # And below the Unreleased marker.
    assert result.index("## [Unreleased]") < result.index("## [1045]")


def test_insert_does_not_prepend_above_title():
    # This is the exact regression: the old code put `entry + upstream`, so the
    # entry preceded the '# Changelog' title. The fixed function never does that.
    upstream = _PREAMBLE + "## [Unreleased]\n\n## [9] - 2026-01-01\n\n- base\n"
    entry = "## [10] - 2026-01-02\n\n- new\n"

    result = tusk_merge._insert_changelog_entry(upstream, entry)

    assert not result.startswith("## [")
    assert result.startswith("# Changelog")


def test_insert_without_unreleased_marker_still_single_title():
    upstream = _PREAMBLE + "## [3] - 2026-01-01\n\n- base\n"
    entry = "## [4] - 2026-01-02\n\n- new\n"

    result = tusk_merge._insert_changelog_entry(upstream, entry)

    assert result.count("# Changelog") == 1
    assert result.startswith("# Changelog")
    assert result.index("## [4]") < result.index("## [3]")


def test_insert_appends_when_no_versioned_section():
    upstream = "# Changelog\n\nAll notable changes.\n"
    entry = "## [1] - 2026-01-01\n\n- first\n"

    result = tusk_merge._insert_changelog_entry(upstream, entry)

    assert result.count("# Changelog") == 1
    assert result.rstrip().endswith("- first")
