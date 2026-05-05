"""Integration test for tusk-upgrade.py --dry-run preview path (Issue #666).

`tusk upgrade --dry-run` must:
  - List every file that would be written/overwritten with byte-size delta
  - Report the resolved VERSION transition (local → remote)
  - List pending migrations with version numbers and docstring summaries
  - Exit without writing any files, running migrations, or committing

This test drives `_run_dry_run_report()` against a fake claude-mode install
layout and asserts both the printed report and that no install-side files
were touched. Mirrors the harness pattern in test_upgrade_dist_excluded_prune.py.
"""

import importlib.util
import json
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

    Writes a tusk-migrate.py with three migrate_N functions so we can assert
    pending-migration detection picks up only the unapplied ones based on the
    fake DB's PRAGMA user_version.
    """
    src = tmp_path / "tusk-v999"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "tusk").write_text("#!/bin/bash\nexit 0\n")
    (src / "bin" / "tusk").chmod(0o755)
    (src / "bin" / "tusk-upgrade.py").write_text("# new upgrader content (longer than installed)\n")
    (src / "bin" / "tusk-keep.py").write_text("# kept across upgrade\n")
    (src / "bin" / "tusk_loader.py").write_text("# new loader\n")
    (src / "bin" / "tusk_skill_filter.py").write_text(
        "def get_project_type(_):\n    return None\n"
        "def filter_manifest(files, _src, _pt):\n    return list(files)\n"
        "def should_install_skill(_dir, _pt):\n    return True\n"
    )
    (src / "bin" / "tusk_github.py").write_text("# stub github helpers\n")
    (src / "bin" / "tusk-migrate.py").write_text(
        "def migrate_98(db, cfg, sd):\n"
        "    \"\"\"Add 'foo' column to 'bar'.\"\"\"\n"
        "    pass\n"
        "def migrate_99(db, cfg, sd):\n"
        "    \"\"\"Backfill 'baz' index.\n\n    Detail line.\n    \"\"\"\n"
        "    pass\n"
        "def migrate_100(db, cfg, sd):\n"
        "    \"\"\"Drop legacy 'qux' view.\"\"\"\n"
        "    pass\n"
        "MIGRATIONS = [(98, migrate_98), (99, migrate_99), (100, migrate_100)]\n"
    )
    (src / "config.default.json").write_text(json.dumps({"domains": [], "agents": []}))
    (src / "pricing.json").write_text("{}\n")
    (src / "VERSION").write_text("999\n")
    (src / "skills" / "tusk").mkdir(parents=True)
    (src / "skills" / "tusk" / "SKILL.md").write_text("# skill body\n")
    (src / ".claude" / "hooks").mkdir(parents=True)
    (src / ".claude" / "hooks" / "setup-path.sh").write_text("#!/bin/bash\nexit 0\n")
    (src / "MANIFEST").write_text(json.dumps([
        ".claude/bin/tusk",
        ".claude/bin/tusk-upgrade.py",
        ".claude/bin/tusk-keep.py",
        ".claude/skills/tusk/SKILL.md",
        ".claude/hooks/setup-path.sh",
    ]))
    return src


def _make_claude_install(tmp_path: Path, user_version: int) -> tuple[Path, Path]:
    """Construct a fake claude-installed project rooted at tmp_path/project.

    Stamps the install-mode marker as claude-consumer, writes an older
    VERSION, and creates an SQLite DB stamped with the requested
    PRAGMA user_version so pending-migration detection has something to
    compare against.
    """
    repo_root = tmp_path / "project"
    script_dir = repo_root / ".claude" / "bin"
    script_dir.mkdir(parents=True)
    (script_dir / "install-mode").write_text("claude-consumer\n")
    (script_dir / "VERSION").write_text("998\n")

    # Pre-existing files at target paths so dry-run can compute size deltas.
    # tusk: stays the same size (zero delta).
    (script_dir / "tusk").write_text("#!/bin/bash\nexit 0\n")
    # tusk-upgrade.py: shorter than tarball copy → positive delta on overwrite.
    (script_dir / "tusk-upgrade.py").write_text("# old\n")

    skills_dir = repo_root / ".claude" / "skills" / "tusk"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("# old skill body\n")

    # Hook does not yet exist locally → flagged as a "new" write.
    # (Intentionally do NOT create .claude/hooks/setup-path.sh.)

    # Live DB at user_version=99 — exactly one migration (98) already applied,
    # so pending should report (99, 100) when MIGRATIONS = [(98), (99), (100)].
    # We use sqlite3 directly here because the test harness needs to seed the
    # PRAGMA user_version that the dry-run reader will query — there is no
    # tusk CLI available in the test sandbox to do this through.
    import sqlite3
    db_dir = repo_root / "tusk"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "tasks.db")
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    conn.close()

    return repo_root, script_dir


def _capture_install_state(repo_root: Path, script_dir: Path) -> dict:
    """Snapshot the (path, mtime, size, content_hash) tuple of every file
    inside the install layout so the test can assert nothing was written."""
    state = {}
    for root in (script_dir, repo_root / ".claude" / "skills", repo_root / ".claude" / "hooks", repo_root / "tusk"):
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file():
                stat = p.stat()
                state[str(p)] = (stat.st_mtime_ns, stat.st_size, p.read_bytes())
    return state


class TestDryRunReport:
    def test_report_lists_files_version_and_pending_migrations(
        self, tmp_path, upgrade_mod, capsys, monkeypatch
    ):
        """Dry-run prints the VERSION transition, file diff, and pending
        migrations — and writes nothing to the install layout."""
        repo_root, script_dir = _make_claude_install(tmp_path, user_version=98)
        src = _make_fake_src(tmp_path)
        monkeypatch.setattr(upgrade_mod, "_verbose", False)

        before = _capture_install_state(repo_root, script_dir)

        upgrade_mod._run_dry_run_report(
            str(src), str(repo_root), str(script_dir),
            local_version=998, remote_version=999,
        )

        out = capsys.readouterr().out

        # VERSION transition surfaced.
        assert "VERSION: 998 → 999" in out
        assert "Dry run — version 998 → 999" in out
        assert "No files will be modified" in out

        # File diff: tusk is overwrite-same-size (delta 0); tusk-upgrade.py is
        # an overwrite with positive delta; setup-path.sh is new.
        assert "+ .claude/hooks/setup-path.sh" in out
        assert "~ .claude/bin/tusk-upgrade.py" in out
        assert "~ .claude/bin/tusk" in out
        # Size delta annotation present.
        assert " bytes," in out

        # Pending migrations: 99 and 100 (98 already applied per user_version).
        assert "Pending migrations (2):" in out
        assert "Migration 99" in out
        assert "Backfill 'baz' index" in out  # docstring first line, period stripped
        assert "Migration 100" in out
        assert "Drop legacy 'qux' view" in out
        # 98 is already applied — must NOT be in the pending list.
        assert "Migration 98" not in out

        # Closing CTA present.
        assert "Re-run without --dry-run to apply." in out

        after = _capture_install_state(repo_root, script_dir)
        assert before == after, (
            "Dry-run must not modify any files in the install layout. "
            f"Differing paths: {set(before) ^ set(after)}; "
            f"changed: {[p for p in before if p in after and before[p] != after[p]]}"
        )

    def test_no_pending_migrations_when_schema_current(
        self, tmp_path, upgrade_mod, capsys, monkeypatch
    ):
        """When PRAGMA user_version >= max migration version, pending list is empty."""
        repo_root, script_dir = _make_claude_install(tmp_path, user_version=100)
        src = _make_fake_src(tmp_path)
        monkeypatch.setattr(upgrade_mod, "_verbose", False)

        upgrade_mod._run_dry_run_report(
            str(src), str(repo_root), str(script_dir),
            local_version=998, remote_version=999,
        )

        out = capsys.readouterr().out
        assert "Pending migrations (0):" in out
        assert "(none — schema is at user_version 100)" in out

    def test_missing_migrate_module_does_not_crash(
        self, tmp_path, upgrade_mod, capsys, monkeypatch
    ):
        """Tarball missing tusk-migrate.py is reported, not raised."""
        repo_root, script_dir = _make_claude_install(tmp_path, user_version=98)
        src = _make_fake_src(tmp_path)
        (src / "bin" / "tusk-migrate.py").unlink()
        monkeypatch.setattr(upgrade_mod, "_verbose", False)

        upgrade_mod._run_dry_run_report(
            str(src), str(repo_root), str(script_dir),
            local_version=998, remote_version=999,
        )

        out = capsys.readouterr().out
        assert "could not load MIGRATIONS from tarball" in out
