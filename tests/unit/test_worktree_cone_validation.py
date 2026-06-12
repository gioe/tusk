"""Unit tests for tracked-path validation of description-derived sparse cone
entries (issue #1044).

Task descriptions routinely mention paths relative to a repo subdirectory
(e.g. "ui/components/ui/button.tsx" meaning apps/web/ui/components/ui/...);
the cone derivation used to treat those as root-relative entries that match
no tracked path. In a repo without a masking sibling cone the bogus entries
silently failed to materialize the intended files. ``_validate_referenced_cone``
now keeps entries that exist (or whose first segment exists at the root),
substitutes entries that uniquely suffix-resolve against tracked directories,
and drops the rest — emitting an advisory at the call site.
"""

import importlib.util
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
WORKTREE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-worktree.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tusk_task_worktree", WORKTREE_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def monorepo(tmp_path):
    """A repo whose files live only under apps/web/ — the issue #1044 shape."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "t@example.test"], cwd=repo)
    _git(["config", "user.name", "T"], cwd=repo)
    for rel in [
        "apps/web/lib/utils.ts",
        "apps/web/ui/components/ui/button.tsx",
        "apps/web/ui/components/cards/entity/card.tsx",
        "docs/README.md",
    ]:
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("x\n", encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    return repo


class TestValidateReferencedCone:
    def test_existing_tracked_entry_is_kept(self, mod, monorepo):
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo), {"apps/web/lib"}
        )
        assert kept == ["apps/web/lib"]
        assert resolved == {}
        assert dropped == []

    def test_subdir_relative_entry_uniquely_resolves(self, mod, monorepo):
        """The issue #1044 incident shape: ui/components/ui exists only under
        apps/web/ and must be substituted, not emitted root-relative."""
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo), {"ui/components/ui"}
        )
        assert kept == []
        assert resolved == {"ui/components/ui": "apps/web/ui/components/ui"}
        assert dropped == []

    def test_unresolvable_entry_is_dropped(self, mod, monorepo):
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo), {"nonexistent/place"}
        )
        assert kept == []
        assert resolved == {}
        assert dropped == ["nonexistent/place"]

    def test_ambiguous_suffix_is_dropped(self, mod, monorepo, tmp_path):
        """Two tracked dirs share the suffix — substitution would guess, so
        the entry is dropped instead."""
        for rel in [
            "apps/web/shared/util/x.ts",
            "apps/api/shared/util/y.ts",
        ]:
            full = monorepo / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("x\n", encoding="utf-8")
        _git(["add", "."], cwd=monorepo)
        _git(["commit", "-m", "more"], cwd=monorepo)

        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo), {"shared/util"}
        )
        assert kept == []
        assert resolved == {}
        assert dropped == ["shared/util"]

    def test_new_subdir_under_existing_root_is_kept(self, mod, monorepo):
        """A task that creates a brand-new subdirectory under an existing
        top-level tree keeps its cone entry."""
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo), {"docs/new-section"}
        )
        assert kept == ["docs/new-section"]
        assert resolved == {}
        assert dropped == []

    def test_untracked_but_on_disk_entry_is_kept(self, mod, monorepo):
        (monorepo / "build" / "out").mkdir(parents=True)
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo), {"build/out"}
        )
        assert kept == ["build/out"]
        assert resolved == {}
        assert dropped == []

    def test_git_failure_keeps_everything(self, mod, tmp_path):
        """Outside a git repo ls-tree fails — validation is skipped and all
        entries pass through untouched."""
        plain = tmp_path / "notarepo"
        plain.mkdir()
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(plain), {"ui/components/ui", "lib/sub"}
        )
        assert sorted(kept) == ["lib/sub", "ui/components/ui"]
        assert resolved == {}
        assert dropped == []

    def test_mixed_incident_shape(self, mod, monorepo):
        """Fully-qualified and subdir-relative mentions together: the
        qualified entry stays, the relative ones resolve or drop."""
        kept, resolved, dropped = mod._validate_referenced_cone(
            str(monorepo),
            {"apps/web/lib", "ui/components/ui", "lib/nowhere"},
        )
        assert kept == ["apps/web/lib"]
        assert resolved == {"ui/components/ui": "apps/web/ui/components/ui"}
        assert dropped == ["lib/nowhere"]


class TestTrackedDirs:
    def test_lists_all_tracked_directories(self, mod, monorepo):
        tracked = mod._tracked_dirs(str(monorepo))
        assert "apps" in tracked
        assert "apps/web/ui/components/ui" in tracked
        assert "docs" in tracked

    def test_returns_none_outside_repo(self, mod, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert mod._tracked_dirs(str(plain)) is None
