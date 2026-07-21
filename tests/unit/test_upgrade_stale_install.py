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
from types import SimpleNamespace


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


def test_copy_bin_files_overwrites_stale_sync_main_helper(tmp_path):
    upgrade = _load_upgrade()

    src = tmp_path / "src"
    src_bin = src / "bin"
    src_bin.mkdir(parents=True)
    (src / "config.default.json").write_text("{}\n", encoding="utf-8")
    (src / "pricing.json").write_text("{}\n", encoding="utf-8")
    (src_bin / "tusk").write_text("#!/bin/sh\n", encoding="utf-8")
    fixed_helper = (
        "def _restore_stash_after_merge_failure(repo_root, stash_message):\n"
        "    return 'fixed'\n"
    )
    (src_bin / "tusk-sync-main.py").write_text(fixed_helper, encoding="utf-8")

    script_dir = tmp_path / "install" / ".claude" / "bin"
    script_dir.mkdir(parents=True)
    (script_dir / "tusk-sync-main.py").write_text(
        "# old ff-only failure branch left owned stash intact\n",
        encoding="utf-8",
    )

    upgrade.copy_bin_files(str(src), str(script_dir))

    assert (script_dir / "tusk-sync-main.py").read_text(encoding="utf-8") == fixed_helper


def test_installed_skills_stale_compares_only_eligible_release_files(
    tmp_path, monkeypatch
):
    upgrade = _load_upgrade()

    src = tmp_path / "src"
    current_skill = src / "skills" / "tusk"
    gated_skill = src / "skills" / "ios-only"
    current_skill.mkdir(parents=True)
    gated_skill.mkdir(parents=True)
    (current_skill / "SKILL.md").write_text("current guidance\n", encoding="utf-8")
    (gated_skill / "SKILL.md").write_text("ios guidance\n", encoding="utf-8")

    repo_root = tmp_path / "project"
    installed = repo_root / ".claude" / "skills" / "tusk" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("current guidance\n", encoding="utf-8")

    skill_filter = SimpleNamespace(
        get_project_type=lambda _repo: "python_service",
        should_install_skill=lambda skill_dir, _project_type: not skill_dir.endswith(
            "ios-only"
        ),
    )
    monkeypatch.setattr(upgrade, "_import_skill_filter", lambda _src: skill_filter)

    assert upgrade._installed_skills_stale(str(src), str(repo_root)) is False

    installed.write_text("stale guidance\n", encoding="utf-8")
    assert upgrade._installed_skills_stale(str(src), str(repo_root)) is True

    installed.unlink()
    assert upgrade._installed_skills_stale(str(src), str(repo_root)) is True


def _upgrade_summary():
    return {
        "install_mode": "claude",
        "manifest_rel": "MANIFEST",
        "hook_summary": {
            "registered": 0,
            "dedup_removed": 0,
            "permissions_added": 0,
        },
        "skill_count": 1,
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


def _same_version_install(tmp_path):
    repo_root = tmp_path / "project"
    script_dir = repo_root / ".claude" / "bin"
    script_dir.mkdir(parents=True)
    (script_dir / "install-mode").write_text("claude-consumer\n", encoding="utf-8")
    (script_dir / "VERSION").write_text("999\n", encoding="utf-8")

    tmp_outer = tmp_path / "download"
    src = tmp_outer / "tusk-v999"
    (src / "bin").mkdir(parents=True)
    (src / "VERSION").write_text("999\n", encoding="utf-8")
    return repo_root, script_dir, src


def test_same_version_refreshes_when_installed_skills_are_stale(
    tmp_path, monkeypatch, capsys
):
    upgrade = _load_upgrade()
    repo_root, script_dir, src = _same_version_install(tmp_path)
    calls = []

    monkeypatch.setattr(upgrade, "is_source_repo", lambda _repo: False)
    monkeypatch.setattr(upgrade, "get_latest_tag", lambda: "v999")
    monkeypatch.setattr(upgrade, "get_remote_version", lambda _tag: 999)
    monkeypatch.setattr(upgrade, "_installed_skills_stale", lambda *_args: True)
    monkeypatch.setattr(
        upgrade,
        "_run_upgrade_steps",
        lambda *args: calls.append(args) or _upgrade_summary(),
    )
    monkeypatch.setattr(upgrade, "check_review_commits_permissions", lambda _repo: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tusk-upgrade.py",
            str(repo_root),
            str(script_dir),
            "--no-commit",
            "--_rexec-src",
            str(src),
        ],
    )

    upgrade.main()

    out = capsys.readouterr().out
    assert "installed skills differ from the current release" in out
    assert "Already up to date" not in out
    assert calls


def test_same_version_current_skills_skip_all_upgrade_steps(
    tmp_path, monkeypatch, capsys
):
    upgrade = _load_upgrade()
    repo_root, script_dir, src = _same_version_install(tmp_path)

    monkeypatch.setattr(upgrade, "is_source_repo", lambda _repo: False)
    monkeypatch.setattr(upgrade, "get_latest_tag", lambda: "v999")
    monkeypatch.setattr(upgrade, "get_remote_version", lambda _tag: 999)
    monkeypatch.setattr(upgrade, "_installed_skills_stale", lambda *_args: False)
    monkeypatch.setattr(
        upgrade,
        "_run_upgrade_steps",
        lambda *_args: (_ for _ in ()).throw(AssertionError("upgrade steps ran")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tusk-upgrade.py",
            str(repo_root),
            str(script_dir),
            "--no-commit",
            "--_rexec-src",
            str(src),
        ],
    )

    upgrade.main()

    out = capsys.readouterr().out
    assert "Already up to date (version 999)." in out
    assert "Upgrade complete" not in out


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
