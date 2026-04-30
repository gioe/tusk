"""Integration test for tusk-upgrade.py dist-excluded prune pass (TASK-220, Issue #601).

When a script is added to bin/dist-excluded.txt, fresh installs (install.sh)
correctly skip it via TUSK_SKIP_SCRIPTS. But before this fix, consumers that
installed an older tusk version retained the now-excluded file forever — the
upgrade flow had no prune step. This test drives `_run_upgrade_steps()` against
a fake claude-mode install layout that mirrors the orphan condition described
in Issue #601 and asserts the file is removed without disturbing unrelated
files inside or outside the install bin/.

Mirrors the harness pattern in test_upgrade_codex_mode.py — fake src tree,
fake install layout, stubbed `subprocess.run` and `update_gitignore` so no real
tusk binary or DB is required.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPGRADE_PATH = REPO_ROOT / "bin" / "tusk-upgrade.py"


@pytest.fixture(scope="module")
def upgrade_mod():
    spec = importlib.util.spec_from_file_location("tusk_upgrade", str(UPGRADE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_fake_src(tmp_path: Path, dist_excluded: list[str]) -> Path:
    """Construct a minimal tarball-extracted src tree with a dist-excluded.txt.

    Mirrors install.sh's expected layout: src/bin/ contains the new tusk
    binary, the new tusk-*.py scripts, tusk_loader.py, and dist-excluded.txt.
    The MANIFEST is claude-shaped (post-translation no-op for claude installs).
    """
    src = tmp_path / "tusk-v999"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "tusk").write_text("#!/bin/bash\nexit 0\n")
    (src / "bin" / "tusk").chmod(0o755)
    (src / "bin" / "tusk-upgrade.py").write_text("# new upgrader\n")
    (src / "bin" / "tusk-keep.py").write_text("# kept across upgrade\n")
    (src / "bin" / "tusk_loader.py").write_text("# new loader\n")
    # Minimal stub satisfying the API tusk-upgrade.py imports during the
    # claude-mode manifest filter step.
    (src / "bin" / "tusk_skill_filter.py").write_text(
        "def get_project_type(_):\n    return None\n"
        "def filter_manifest(files, _src, _pt):\n    return list(files)\n"
        "def should_install_skill(_dir, _pt):\n    return True\n"
    )
    # GitHub release tarballs include every bin/tusk-*.py from the source repo,
    # including the ones listed in dist-excluded.txt — so the upgrade flow has
    # to skip them in copy_bin_files() *and* prune them from the install bin/.
    # Mirror that here so removing either guard would fail an assertion below.
    for excluded in dist_excluded:
        (src / "bin" / excluded).write_text("# present in tarball but excluded from distribution\n")
    (src / "bin" / "dist-excluded.txt").write_text(
        "\n".join(dist_excluded) + ("\n" if dist_excluded else "")
    )
    (src / "config.default.json").write_text(json.dumps({"domains": [], "agents": []}))
    (src / "pricing.json").write_text("{}\n")
    (src / "VERSION").write_text("999\n")
    (src / "skills" / "tusk").mkdir(parents=True)
    (src / "skills" / "tusk" / "SKILL.md").write_text("# placeholder skill\n")
    (src / ".claude" / "hooks").mkdir(parents=True)
    (src / ".claude" / "hooks" / "setup-path.sh").write_text("#!/bin/bash\nexit 0\n")
    (src / "MANIFEST").write_text(json.dumps([
        ".claude/bin/tusk",
        ".claude/bin/tusk-upgrade.py",
        ".claude/bin/tusk-keep.py",
        ".claude/bin/tusk_loader.py",
        ".claude/bin/tusk_skill_filter.py",
        ".claude/bin/config.default.json",
        ".claude/bin/pricing.json",
        ".claude/bin/VERSION",
        ".claude/skills/tusk/SKILL.md",
        ".claude/hooks/setup-path.sh",
    ]))
    return src


def _make_claude_install(tmp_path: Path) -> tuple[Path, Path]:
    """Construct a fake claude-installed project rooted at tmp_path/project.

    Returns (repo_root, script_dir). Stamps the install-mode marker as
    claude-consumer (the form install.sh has written since role detection
    landed) and pre-populates an older VERSION so the upgrade actually runs.
    """
    repo_root = tmp_path / "project"
    script_dir = repo_root / ".claude" / "bin"
    script_dir.mkdir(parents=True)
    (script_dir / "install-mode").write_text("claude-consumer\n")
    (script_dir / "VERSION").write_text("998\n")
    (repo_root / "tusk").mkdir(parents=True)
    (repo_root / "tusk" / "config.json").write_text("{}\n")
    return repo_root, script_dir


def _stub_side_effects(monkeypatch, upgrade_mod):
    """Stub subprocess.run and update_gitignore (no real tusk CLI in tests)."""
    class _FakeResult:
        returncode = 0
        stdout = "schema up to date\n"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(upgrade_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade_mod, "update_gitignore", lambda script_dir: None)
    monkeypatch.setattr(upgrade_mod, "_verbose", False)


class TestDistExcludedPrune:
    def test_orphan_dist_excluded_file_is_removed_on_upgrade(
        self, tmp_path, upgrade_mod, monkeypatch
    ):
        """Issue #601 repro: a previously-installed file now listed in
        dist-excluded.txt is removed by `tusk upgrade`. Mirrors the failing
        test from the issue body — after upgrade, the orphan file is gone."""
        repo_root, script_dir = _make_claude_install(tmp_path)
        src = _make_fake_src(tmp_path, dist_excluded=["tusk-generate-manifest.py"])
        tmpdir = tmp_path / "scratch"
        tmpdir.mkdir()
        _stub_side_effects(monkeypatch, upgrade_mod)

        orphan_path = script_dir / "tusk-generate-manifest.py"
        orphan_path.write_text("# left behind by an older tusk install\n")
        assert orphan_path.exists(), "Test setup: orphan must exist before upgrade"

        summary = upgrade_mod._run_upgrade_steps(
            str(src), str(repo_root), str(script_dir), str(tmpdir)
        )

        assert summary["pruned_count"] == 1, (
            f"Expected pruned_count=1 for the single dist-excluded entry, "
            f"got {summary['pruned_count']}"
        )
        assert not orphan_path.exists(), (
            "Orphan dist-excluded file should have been pruned by upgrade. "
            "If pruned_count is correct but the file is back, copy_bin_files() "
            "is re-installing it from the tarball — the dist-excluded skip in "
            "copy_bin_files must run alongside prune_dist_excluded."
        )

    def test_files_outside_install_bin_are_never_touched(
        self, tmp_path, upgrade_mod, monkeypatch
    ):
        """Criterion 965: the prune pass operates only on basenames inside the
        install bin/. A same-named file elsewhere in the repo (or even at repo
        root) survives the upgrade unscathed."""
        repo_root, script_dir = _make_claude_install(tmp_path)
        src = _make_fake_src(tmp_path, dist_excluded=["tusk-generate-manifest.py"])
        tmpdir = tmp_path / "scratch"
        tmpdir.mkdir()
        _stub_side_effects(monkeypatch, upgrade_mod)

        # In-bin orphan: should be removed.
        in_bin_orphan = script_dir / "tusk-generate-manifest.py"
        in_bin_orphan.write_text("# in-bin orphan\n")

        # Same-basename file at repo root: must NOT be touched. The prune pass
        # only looks under <repo_root>/.claude/bin/, so this is the canary for
        # criterion 965 (files outside install bin/ are never touched).
        outside_bin = repo_root / "tusk-generate-manifest.py"
        outside_bin.write_text("# user file at repo root, unrelated to tusk install\n")

        # Unrelated file inside install bin/: must survive (not in dist-excluded).
        unrelated = script_dir / "tusk-keep.py"
        unrelated.write_text("# pre-existing unrelated file\n")

        upgrade_mod._run_upgrade_steps(
            str(src), str(repo_root), str(script_dir), str(tmpdir)
        )

        assert not in_bin_orphan.exists(), "In-bin orphan should be pruned"
        assert outside_bin.exists(), (
            "Files outside the install bin/ must never be touched by the prune pass"
        )
        assert outside_bin.read_text() == "# user file at repo root, unrelated to tusk install\n"
        assert unrelated.exists(), (
            "Unrelated files inside install bin/ (not in dist-excluded.txt) must survive"
        )

    def test_no_op_when_dist_excluded_missing(
        self, tmp_path, upgrade_mod, monkeypatch
    ):
        """Older tarballs predate dist-excluded.txt — upgrade must degrade to a
        no-op rather than erroring out."""
        repo_root, script_dir = _make_claude_install(tmp_path)
        src = _make_fake_src(tmp_path, dist_excluded=[])
        os.remove(src / "bin" / "dist-excluded.txt")
        tmpdir = tmp_path / "scratch"
        tmpdir.mkdir()
        _stub_side_effects(monkeypatch, upgrade_mod)

        # Pre-existing file that would be pruned if dist-excluded.txt listed it.
        survivor = script_dir / "tusk-generate-manifest.py"
        survivor.write_text("# unchanged by upgrade since dist-excluded is absent\n")

        summary = upgrade_mod._run_upgrade_steps(
            str(src), str(repo_root), str(script_dir), str(tmpdir)
        )

        assert summary["pruned_count"] == 0, (
            f"Expected pruned_count=0 when dist-excluded.txt is absent, "
            f"got {summary['pruned_count']}"
        )
        assert survivor.exists(), (
            "Files must not be removed when dist-excluded.txt is absent"
        )
