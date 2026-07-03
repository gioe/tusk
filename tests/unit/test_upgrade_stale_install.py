"""Regression tests for stale installed binaries during ``tusk upgrade``.

Issue #1140 reported an installed copy whose VERSION appeared current while
the installed helpers were stale relative to the live DB schema. ``upgrade``
must not declare "Already up to date" before checking whether the installed
schema support can actually read the live database.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
UPGRADE_PATH = REPO_ROOT / "bin" / "tusk-upgrade.py"


def _load_upgrade():
    spec = importlib.util.spec_from_file_location("tusk_upgrade_stale", UPGRADE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_migrate(path: Path, max_version: int) -> None:
    lines = ["MIGRATIONS = [\n"]
    for version in range(1, max_version + 1):
        lines.append(f"    ({version}, migrate_{version}),\n")
    lines.append("]\n")
    path.write_text("".join(lines), encoding="utf-8")


def test_no_commit_does_not_claim_current_when_schema_support_is_stale(
    tmp_path, monkeypatch, capsys
):
    upgrade = _load_upgrade()

    repo_root = tmp_path / "project"
    script_dir = repo_root / ".claude" / "bin"
    script_dir.mkdir(parents=True)
    (script_dir / "install-mode").write_text("claude-consumer\n", encoding="utf-8")
    (script_dir / "VERSION").write_text("999\n", encoding="utf-8")
    _write_migrate(script_dir / "tusk-migrate.py", max_version=98)

    db_dir = repo_root / "tusk"
    db_dir.mkdir()
    conn = sqlite3.connect(db_dir / "tasks.db")
    conn.execute("PRAGMA user_version = 100")
    conn.commit()
    conn.close()

    tmp_outer = tmp_path / "download"
    src = tmp_outer / "tusk-v999"
    (src / "bin").mkdir(parents=True)
    (src / "VERSION").write_text("999\n", encoding="utf-8")
    _write_migrate(src / "bin" / "tusk-migrate.py", max_version=100)

    calls = []

    def fake_run_upgrade_steps(src_arg, repo_arg, script_arg, tmpdir_arg):
        calls.append((src_arg, repo_arg, script_arg, tmpdir_arg))
        return {
            "install_mode": "claude",
            "manifest_rel": "MANIFEST",
            "hook_summary": {
                "registered": 0,
                "dedup_removed": 0,
                "permissions_added": 0,
            },
            "skill_count": 0,
            "hook_count": 0,
            "script_count": 0,
            "added_perms": [],
            "backfilled_keys": [],
            "migrate_summary": "skipped",
            "orphan_count": 0,
            "pruned_count": 0,
            "deprecated_count": 0,
            "newline_fixes": 0,
        }

    monkeypatch.setattr(upgrade, "is_source_repo", lambda _repo: False)
    monkeypatch.setattr(upgrade, "get_latest_tag", lambda: "v999")
    monkeypatch.setattr(upgrade, "get_remote_version", lambda _tag: 999)
    monkeypatch.setattr(upgrade, "_run_upgrade_steps", fake_run_upgrade_steps)
    monkeypatch.setattr(upgrade, "check_review_commits_permissions", lambda _repo: [])

    monkeypatch.setattr(sys, "argv", [
        "tusk-upgrade.py",
        str(repo_root),
        str(script_dir),
        "--no-commit",
        "--_rexec-src",
        str(src),
    ])
    upgrade.main()

    out = capsys.readouterr().out
    assert "Already up to date" not in out
    assert "Upgrade complete (version 999)." in out
    assert calls, "stale schema support must force the upgrade path"
