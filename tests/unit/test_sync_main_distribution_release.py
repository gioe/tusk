"""Release guard for the sync-main stash restoration fix.

Issue #1170 reported that an installed v1209 copy still had the old
sync-main ff-only merge failure behavior. The source fix is present, so
the distribution version must advance beyond v1209 for normal upgrades to
copy the fixed helper into consumer installs.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sync_main_stash_restore_fix_released_after_v1209():
    version = int((REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip())

    assert version > 1209
