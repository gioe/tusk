"""Unit tests for path-style entries in worktree.symlink_files (issue #867).

The walker in bin/tusk-task-worktree.py originally matched configured entries
against bare basenames yielded by os.walk, so a monorepo-scoped entry like
"apps/web/node_modules" silently never matched. The fix partitions entries:
bare basenames keep the walk-and-match behavior, path-style entries (containing
"/") are treated as project-relative paths and link exactly once.
"""

import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKTREE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-worktree.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_task_worktree", WORKTREE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _make_primary(tmp_path, files):
    primary = tmp_path / "primary"
    primary.mkdir()
    for rel in files:
        full = primary / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("x")
    return primary


def _make_worktree(tmp_path):
    wt = tmp_path / "worktree"
    wt.mkdir()
    return wt


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks behave differently on Windows")
class TestPathStyleEntries:
    def test_path_style_hit_creates_exactly_one_symlink(self, tmp_path, mod):
        primary = _make_primary(
            tmp_path,
            [
                "apps/web/node_modules/.bin/vitest",
                "apps/other/node_modules/.bin/jest",
            ],
        )
        wt = _make_worktree(tmp_path)

        created = mod._link_gitignored_files(
            str(primary), str(wt), ["apps/web/node_modules"]
        )

        wt_target = wt / "apps" / "web" / "node_modules"
        wt_other = wt / "apps" / "other" / "node_modules"
        assert wt_target.is_symlink()
        assert not wt_other.exists() and not wt_other.is_symlink()
        assert os.readlink(str(wt_target)) == str(primary / "apps/web/node_modules")
        assert len(created) == 1
        assert created[0]["dst"] == str(wt_target)

    def test_path_style_miss_skips_silently(self, tmp_path, mod):
        primary = _make_primary(tmp_path, ["apps/web/.gitkeep"])
        wt = _make_worktree(tmp_path)

        created = mod._link_gitignored_files(
            str(primary), str(wt), ["apps/web/node_modules"]
        )

        assert created == []
        assert not (wt / "apps" / "web" / "node_modules").exists()

    def test_bare_basename_baseline_unchanged(self, tmp_path, mod):
        primary = _make_primary(
            tmp_path,
            [".venv/bin/python", "apps/scraper/.venv/bin/python"],
        )
        wt = _make_worktree(tmp_path)

        created = mod._link_gitignored_files(str(primary), str(wt), [".venv"])

        wt_top = wt / ".venv"
        wt_nested = wt / "apps" / "scraper" / ".venv"
        assert wt_top.is_symlink()
        assert wt_nested.is_symlink()
        assert {c["dst"] for c in created} == {str(wt_top), str(wt_nested)}

    def test_mixed_list_basename_and_path_style(self, tmp_path, mod):
        primary = _make_primary(
            tmp_path,
            [
                ".env",
                "apps/web/node_modules/.bin/vitest",
                "apps/scraper/.venv/bin/python",
                "apps/api/node_modules/.bin/tsx",
            ],
        )
        wt = _make_worktree(tmp_path)

        created = mod._link_gitignored_files(
            str(primary),
            str(wt),
            [".env", "apps/web/node_modules"],
        )

        # .env (basename) — matched at the one place it exists.
        assert (wt / ".env").is_symlink()
        # apps/web/node_modules (path-style) — matched exactly once.
        assert (wt / "apps" / "web" / "node_modules").is_symlink()
        # apps/api/node_modules — NOT linked (path-style entry was apps/web, not bare basename).
        assert not (wt / "apps" / "api" / "node_modules").exists()
        # apps/scraper/.venv — NOT linked (.venv is not in the list).
        assert not (wt / "apps" / "scraper" / ".venv").exists()
        assert len(created) == 2

    def test_path_style_does_not_overmatch_nested_basename(self, tmp_path, mod):
        """The whole point of path-style: scope a link to ONE subdir without
        symlinking every nested basename. A bare 'node_modules' would link all
        three copies; 'apps/web/node_modules' links only one.
        """
        primary = _make_primary(
            tmp_path,
            [
                "apps/web/node_modules/x",
                "apps/api/node_modules/y",
                "tools/node_modules/z",
            ],
        )
        wt = _make_worktree(tmp_path)

        created = mod._link_gitignored_files(
            str(primary), str(wt), ["apps/web/node_modules"]
        )

        assert (wt / "apps" / "web" / "node_modules").is_symlink()
        assert not (wt / "apps" / "api" / "node_modules").exists()
        assert not (wt / "tools" / "node_modules").exists()
        assert len(created) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks behave differently on Windows")
class TestPathStyleValidation:
    """Path-style entries must not escape the primary checkout or yield
    ambiguous targets. Rejected entries are dropped silently — consistent with
    the existing best-effort failure mode for unreachable basenames.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "/etc/passwd",            # absolute
            "../outside",             # path-escape via leading ..
            "apps/../../escape",      # path-escape via interior ..
            "apps//web",              # empty segment
            "apps/web/",              # trailing slash → empty segment
            "apps/./web",             # interior "."
        ],
    )
    def test_rejected_entries_create_no_symlinks(self, tmp_path, mod, bad):
        primary = _make_primary(tmp_path, ["apps/web/marker"])
        wt = _make_worktree(tmp_path)

        created = mod._link_gitignored_files(str(primary), str(wt), [bad])

        assert created == []

    def test_destination_already_exists_skips(self, tmp_path, mod):
        """If the worktree already has something at the target path (a real
        file, dir, or a pre-existing symlink), the walker skips it — no
        overwrite. Same invariant as the bare-basename path.
        """
        primary = _make_primary(tmp_path, ["apps/web/node_modules/marker"])
        wt = _make_worktree(tmp_path)
        # Pre-create the destination as a regular directory.
        (wt / "apps" / "web" / "node_modules").mkdir(parents=True)

        created = mod._link_gitignored_files(
            str(primary), str(wt), ["apps/web/node_modules"]
        )

        assert created == []
        assert not (wt / "apps" / "web" / "node_modules").is_symlink()
