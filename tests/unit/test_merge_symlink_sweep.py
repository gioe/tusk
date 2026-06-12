"""Unit tests for ``_sweep_primary_targeted_symlinks`` (issue #1077).

The name-based ``_clean_tusk_auto_symlinks`` pre-clean only knows the
configured / canonical symlink set. Symlinks into the primary checkout can
also be created at unconfigured locations (the linked-worktree test gate's
on-demand node_modules link, issue #1067; an agent's ad-hoc ``ln -s``), and
they block ``git worktree remove`` the same way. The sweep is the
failure-path companion: it unlinks every symlink whose resolved target lies
inside the primary checkout — recoverable by definition — and leaves
everything else alone.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
MERGE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-merge.py")


def _load_module():
    tusk_loader_mock = MagicMock()
    db_lib_mock = MagicMock()
    db_lib_mock.get_connection = MagicMock()
    db_lib_mock.checkpoint_wal = MagicMock()
    tusk_loader_mock.load.return_value = db_lib_mock
    with patch.dict("sys.modules", {"tusk_loader": tusk_loader_mock}):
        spec = importlib.util.spec_from_file_location("tusk_merge", MERGE_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _setup(tmp_path):
    primary = tmp_path / "primary"
    (primary / "real_nm").mkdir(parents=True)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    return primary, workspace


class TestSweepPrimaryTargetedSymlinks:
    def test_removes_top_level_symlink_into_primary(self, tmp_path):
        mod = _load_module()
        primary, workspace = _setup(tmp_path)
        link = workspace / "node_modules"
        link.symlink_to(primary / "real_nm")

        removed = mod._sweep_primary_targeted_symlinks(
            str(workspace), str(primary)
        )

        assert removed == 1
        assert not os.path.lexists(link)
        # The target inside the primary is untouched.
        assert (primary / "real_nm").is_dir()

    def test_removes_nested_unconfigured_symlink(self, tmp_path):
        """A symlink at a monorepo subdir path that no config entry names
        (the issue #1067 test-gate shape) is still swept."""
        mod = _load_module()
        primary, workspace = _setup(tmp_path)
        nested = workspace / "apps" / "web"
        nested.mkdir(parents=True)
        link = nested / "node_modules"
        link.symlink_to(primary / "real_nm")

        removed = mod._sweep_primary_targeted_symlinks(
            str(workspace), str(primary)
        )

        assert removed == 1
        assert not os.path.lexists(link)

    def test_removes_dangling_symlink_into_primary(self, tmp_path):
        """A dangling symlink whose (missing) target path is inside the
        primary is still tusk-shaped and still swept."""
        mod = _load_module()
        primary, workspace = _setup(tmp_path)
        link = workspace / ".venv"
        link.symlink_to(primary / "no-such-dir")

        removed = mod._sweep_primary_targeted_symlinks(
            str(workspace), str(primary)
        )

        assert removed == 1
        assert not os.path.lexists(link)

    def test_preserves_symlink_targeting_outside_primary(self, tmp_path):
        mod = _load_module()
        primary, workspace = _setup(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = workspace / "external"
        link.symlink_to(elsewhere)

        removed = mod._sweep_primary_targeted_symlinks(
            str(workspace), str(primary)
        )

        assert removed == 0
        assert os.path.lexists(link)

    def test_preserves_real_files_and_dirs(self, tmp_path):
        mod = _load_module()
        primary, workspace = _setup(tmp_path)
        real_dir = workspace / "node_modules"
        real_dir.mkdir()
        (real_dir / "pkg.js").write_text("x\n", encoding="utf-8")
        real_file = workspace / "notes.txt"
        real_file.write_text("keep\n", encoding="utf-8")

        removed = mod._sweep_primary_targeted_symlinks(
            str(workspace), str(primary)
        )

        assert removed == 0
        assert (real_dir / "pkg.js").exists()
        assert real_file.exists()

    def test_missing_workspace_returns_zero(self, tmp_path):
        mod = _load_module()
        primary, _ = _setup(tmp_path)
        removed = mod._sweep_primary_targeted_symlinks(
            str(tmp_path / "gone"), str(primary)
        )
        assert removed == 0


class TestListWorktreeEntries:
    def test_lists_entries_excluding_git(self, tmp_path):
        mod = _load_module()
        workspace = tmp_path / "wt"
        (workspace / ".git").mkdir(parents=True)
        (workspace / "leftover").mkdir()
        (workspace / "stray.txt").write_text("x\n", encoding="utf-8")

        entries = mod._list_worktree_entries(str(workspace))

        assert entries == ["leftover", "stray.txt"]

    def test_respects_limit(self, tmp_path):
        mod = _load_module()
        workspace = tmp_path / "wt"
        workspace.mkdir()
        for i in range(15):
            (workspace / f"f{i:02d}").write_text("x\n", encoding="utf-8")

        entries = mod._list_worktree_entries(str(workspace), limit=10)

        assert len(entries) == 10

    def test_missing_workspace_returns_empty(self, tmp_path):
        mod = _load_module()
        assert mod._list_worktree_entries(str(tmp_path / "gone")) == []
