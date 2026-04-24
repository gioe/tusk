"""End-to-end integration tests for tusk-upgrade.py Codex-mode flow (TASK-145).

Drives `_run_upgrade_steps()` against a fake Codex install layout and a fake
tarball-extracted src tree so the full orchestration (install-mode detection,
manifest translation, claude-only step gating, orphan removal, manifest write,
VERSION stamp) is exercised without hitting GitHub or requiring a real tusk
binary.

Unit coverage for the two pure helpers (`detect_install_mode`,
`translate_manifest_for_mode`) lives in tests/unit/test_upgrade_codex_mode.py;
this file covers the orchestration that wires them together.
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


def _make_fake_src(tmp_path: Path) -> Path:
    """Construct a minimal tarball-extracted src tree.

    Mirrors the layout produced by `tar x` on a GitHub release tarball:
    src/bin/{tusk, tusk-*.py, tusk_loader.py}, src/config.default.json,
    src/pricing.json, src/VERSION, src/MANIFEST. The MANIFEST is always
    claude-shaped — it's the upgrade flow's job to translate it for Codex.
    """
    src = tmp_path / "tusk-v999"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "tusk").write_text("#!/bin/bash\nexit 0\n")
    (src / "bin" / "tusk").chmod(0o755)
    (src / "bin" / "tusk-upgrade.py").write_text("# new upgrader\n")
    (src / "bin" / "tusk-example.py").write_text("# new helper\n")
    (src / "bin" / "tusk_loader.py").write_text("# new loader\n")
    (src / "config.default.json").write_text(json.dumps({"domains": [], "agents": []}))
    (src / "pricing.json").write_text("{}\n")
    (src / "VERSION").write_text("999\n")
    # Populate the claude-only source trees so the gating assertion below
    # (no .claude/ created in repo_root) is load-bearing: without the
    # install_mode == "claude" guard, copy_skills/copy_hooks would happily
    # create .claude/skills/ and .claude/hooks/ under the codex project root.
    (src / "skills" / "tusk").mkdir(parents=True)
    (src / "skills" / "tusk" / "SKILL.md").write_text("# placeholder skill\n")
    (src / ".claude" / "hooks").mkdir(parents=True)
    (src / ".claude" / "hooks" / "setup-path.sh").write_text("#!/bin/bash\nexit 0\n")
    (src / "MANIFEST").write_text(json.dumps([
        ".claude/bin/tusk",
        ".claude/bin/tusk-upgrade.py",
        ".claude/bin/tusk-example.py",
        ".claude/bin/tusk_loader.py",
        ".claude/bin/config.default.json",
        ".claude/bin/pricing.json",
        ".claude/bin/VERSION",
        ".claude/skills/tusk/SKILL.md",
        ".claude/hooks/setup-path.sh",
    ]))
    return src


def _make_codex_install(tmp_path: Path) -> tuple[Path, Path]:
    """Construct a fake Codex-installed project rooted at tmp_path/project.

    Returns (repo_root, script_dir) — tusk/bin/ stamped with install-mode=codex,
    an older VERSION, and a minimal tusk/config.json so merge_config_defaults
    actually runs its backfill branch.
    """
    repo_root = tmp_path / "project"
    script_dir = repo_root / "tusk" / "bin"
    script_dir.mkdir(parents=True)
    (script_dir / "install-mode").write_text("codex\n")
    (script_dir / "VERSION").write_text("998\n")
    (repo_root / "tusk" / "config.json").write_text("{}\n")
    return repo_root, script_dir


def _stub_side_effects(monkeypatch, upgrade_mod):
    """Stub the two side-effects in _run_upgrade_steps that need a real tusk CLI.

    - subprocess.run — called once for `tusk migrate` using the newly installed
      binary. Our fake tusk is a bash stub with no DB, so we return a zero-exit
      result directly.
    - update_gitignore — invokes `tusk update-gitignore` via subprocess; stub to
      a no-op so the test doesn't care about the gitignore file.
    """
    class _FakeResult:
        returncode = 0
        stdout = "schema up to date\n"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(upgrade_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade_mod, "update_gitignore", lambda script_dir: None)
    # Keep test output clean; verbose=False also exercises the quiet-mode branch
    # of _run_upgrade_steps (capture_output path for migrate).
    monkeypatch.setattr(upgrade_mod, "_verbose", False)


class TestCodexUpgradeEndToEnd:
    def test_upgrade_lands_in_tusk_bin_and_leaves_claude_alone(
        self, tmp_path, upgrade_mod, monkeypatch
    ):
        """Criterion 622: Codex-mode upgrade updates tusk/bin/, writes
        tusk/tusk-manifest.json with only tusk/bin/ paths, and does not touch
        .claude/ (no skills, no hooks, no settings.json)."""
        repo_root, script_dir = _make_codex_install(tmp_path)
        src = _make_fake_src(tmp_path)
        tmpdir = tmp_path / "scratch"
        tmpdir.mkdir()
        _stub_side_effects(monkeypatch, upgrade_mod)

        summary = upgrade_mod._run_upgrade_steps(
            str(src), str(repo_root), str(script_dir), str(tmpdir)
        )

        assert summary["install_mode"] == "codex"
        assert summary["manifest_rel"] == "tusk/tusk-manifest.json"

        assert (script_dir / "tusk").exists()
        assert os.access(str(script_dir / "tusk"), os.X_OK)
        for name in [
            "tusk-upgrade.py", "tusk-example.py", "tusk_loader.py",
            "config.default.json", "pricing.json", "VERSION",
        ]:
            assert (script_dir / name).exists(), f"{name} missing from tusk/bin/"
        assert (script_dir / "VERSION").read_text().strip() == "999"

        manifest_path = repo_root / "tusk" / "tusk-manifest.json"
        assert manifest_path.exists(), "Codex upgrade must write tusk/tusk-manifest.json"
        entries = json.loads(manifest_path.read_text())
        assert all(not e.startswith(".claude/") for e in entries), (
            f"Codex manifest must not contain any .claude/ paths, got: {entries}"
        )
        assert "tusk/bin/tusk" in entries
        assert "tusk/bin/tusk-upgrade.py" in entries

        assert not (repo_root / ".claude").exists(), (
            "Codex-mode upgrade must not create .claude/ directory"
        )

        assert summary["skill_count"] == 0
        assert summary["hook_count"] == 0
        assert summary["hook_summary"] == {
            "registered": 0, "dedup_removed": 0, "permissions_added": 0,
        }
        assert summary["added_perms"] == []
