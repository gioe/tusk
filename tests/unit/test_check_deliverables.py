"""Unit tests for tusk-check-deliverables.py.

Covers:
- _extract_paths: path extraction from free-form text
- find_existing_files: returns only paths that exist on disk
- main: correct JSON shape and recommendation for both found/not-found cases
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_check_deliverables",
    os.path.join(BIN, "tusk-check-deliverables.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# extract_paths and friends were hoisted into tusk-git-helpers.py (issue #627);
# tusk-check-deliverables now imports them as public names. Tests reference the
# public names directly via the loaded module.
_extract_paths = mod.extract_paths


# ── helpers ───────────────────────────────────────────────────────────


def _make_db(tmp_path, task_id=99, summary="Create /foo skill", description="", criteria=None):
    """Create a minimal tasks + acceptance_criteria DB and return its path.

    DB is placed at tmp_path/tusk/tasks.db so that repo_root resolves to
    tmp_path (two dirname() calls up), matching the real tusk layout.
    """
    tusk_dir = tmp_path / "tusk"
    tusk_dir.mkdir(exist_ok=True)
    db_path = str(tusk_dir / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            description TEXT,
            status TEXT DEFAULT 'To Do'
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            criterion TEXT,
            verification_spec TEXT
        );
    """)
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
        (task_id, summary, description),
    )
    for crit, spec in (criteria or []):
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, verification_spec) VALUES (?, ?, ?)",
            (task_id, crit, spec),
        )
    conn.commit()
    conn.close()
    return db_path


# ── _extract_paths ────────────────────────────────────────────────────


class TestExtractPaths:
    def test_extracts_skills_path(self):
        text = "Skill lives at skills/foo/SKILL.md"
        paths = _extract_paths(text)
        assert "skills/foo/SKILL.md" in paths

    def test_extracts_dotclaude_path(self):
        text = "Install to .claude/skills/bar/SKILL.md on disk"
        paths = _extract_paths(text)
        assert ".claude/skills/bar/SKILL.md" in paths

    def test_extracts_bin_path(self):
        text = "New script at bin/tusk-foo.py"
        paths = _extract_paths(text)
        assert "bin/tusk-foo.py" in paths

    def test_ignores_bare_directory(self):
        text = "Put it in skills/foo/ with no filename"
        paths = _extract_paths(text)
        # A bare directory (no extension) should not appear
        assert not any(p.endswith("/") and "." not in os.path.basename(p.rstrip("/")) for p in paths)

    def test_returns_empty_for_empty_string(self):
        assert _extract_paths("") == []

    def test_returns_empty_for_none(self):
        assert _extract_paths(None) == []

    def test_extracts_arbitrary_path_outside_known_prefixes(self):
        text = "See config/foo.yml for settings"
        paths = _extract_paths(text)
        assert "config/foo.yml" in paths

    def test_does_not_extract_urls(self):
        text = "See https://example.com/foo.yml for docs"
        paths = _extract_paths(text)
        assert not any("://" in p for p in paths)

    def test_extracts_nested_arbitrary_path(self):
        text = "Edit custom/app/settings.py to fix the issue"
        paths = _extract_paths(text)
        assert "custom/app/settings.py" in paths

    def test_deduplication_in_find_existing_files(self, tmp_path):
        """Same path mentioned twice should appear once in candidates."""
        # Create the file so it passes the existence check
        skill_dir = tmp_path / "skills" / "dup"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# dup")

        db_path = _make_db(
            tmp_path,
            summary="skills/dup/SKILL.md",
            description="Also see skills/dup/SKILL.md for details",
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        found = mod.find_existing_files(99, conn, str(tmp_path))
        conn.close()
        assert found.count("skills/dup/SKILL.md") == 1


# ── find_existing_files ───────────────────────────────────────────────


class TestFindExistingFiles:
    def test_returns_file_from_summary_when_it_exists(self, tmp_path):
        skill_dir = tmp_path / "skills" / "myplugin"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# myplugin")

        db_path = _make_db(tmp_path, summary="Create skills/myplugin/SKILL.md")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        found = mod.find_existing_files(99, conn, str(tmp_path))
        conn.close()
        assert "skills/myplugin/SKILL.md" in found

    def test_returns_empty_when_file_absent(self, tmp_path):
        db_path = _make_db(tmp_path, summary="Create skills/missing/SKILL.md")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        found = mod.find_existing_files(99, conn, str(tmp_path))
        conn.close()
        assert found == []

    def test_finds_file_from_criteria_spec(self, tmp_path):
        skill_dir = tmp_path / "skills" / "newskill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# newskill")

        db_path = _make_db(
            tmp_path,
            criteria=[("Skill file exists", "skills/newskill/SKILL.md")],
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        found = mod.find_existing_files(99, conn, str(tmp_path))
        conn.close()
        assert "skills/newskill/SKILL.md" in found

    def test_returns_empty_for_nonexistent_task(self, tmp_path):
        db_path = _make_db(tmp_path, task_id=99)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        found = mod.find_existing_files(999, conn, str(tmp_path))
        conn.close()
        assert found == []


# ── main (via subprocess) ─────────────────────────────────────────────


def _run_main(db_path, task_id, config_path="fake.json"):
    """Run tusk-check-deliverables.py via subprocess, return (returncode, parsed_json)."""
    result = subprocess.run(
        [sys.executable, os.path.join(BIN, "tusk-check-deliverables.py"),
         db_path, config_path, str(task_id)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


class TestMainRecommendations:
    def test_implement_fresh_when_no_commits_no_files(self, tmp_path):
        """No commits anywhere, no matching files → implement_fresh."""
        db_path = _make_db(tmp_path, task_id=9991, summary="Add bin/nonexistent-file.py")
        # Use tmp_path as repo_root — git log there will find no TASK-9991 commits
        rc, stdout, _ = _run_main(db_path, 9991)
        assert rc == 0
        data = json.loads(stdout)
        assert data["commits_found"] is False
        assert data["files_found"] is False
        assert data["recommendation"] == "implement_fresh"

    def test_mark_done_when_file_exists(self, tmp_path):
        """No commits, but referenced file exists → mark_done."""
        skill_dir = tmp_path / "skills" / "present"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# present")

        db_path = _make_db(
            tmp_path,
            task_id=9992,
            summary="Create skills/present/SKILL.md",
        )
        rc, stdout, _ = _run_main(db_path, 9992)
        assert rc == 0
        data = json.loads(stdout)
        assert data["commits_found"] is False
        assert data["files_found"] is True
        assert "skills/present/SKILL.md" in data["files"]
        assert data["recommendation"] == "mark_done"

    def test_output_is_valid_json(self, tmp_path):
        db_path = _make_db(tmp_path, task_id=9993)
        rc, stdout, _ = _run_main(db_path, 9993)
        assert rc == 0
        data = json.loads(stdout)
        assert set(data.keys()) == {
            "commits_found",
            "files_found",
            "files",
            "default_branch_commits",
            "default_branch_commit_files",
            "recommendation",
        }

    def test_error_on_unknown_task(self, tmp_path):
        db_path = _make_db(tmp_path, task_id=1)
        rc, _, stderr = _run_main(db_path, 9999)
        assert rc == 1
        assert "not found" in stderr

    def test_accepts_task_prefix_form(self, tmp_path):
        db_path = _make_db(tmp_path, task_id=9994)
        rc, stdout, _ = _run_main(db_path, "TASK-9994")
        assert rc == 0
        json.loads(stdout)  # just verify it's valid JSON

    def test_implement_fresh_includes_empty_default_branch_commits(self, tmp_path):
        """The existing implement_fresh path must still emit an empty default_branch_commits list."""
        db_path = _make_db(tmp_path, task_id=9996, summary="Add bin/nonexistent-file.py")
        rc, stdout, _ = _run_main(db_path, 9996)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "implement_fresh"
        assert data["default_branch_commits"] == []
        assert data["default_branch_commit_files"] == []

    def test_mark_done_includes_empty_default_branch_commits(self, tmp_path):
        """The existing mark_done path must still emit an empty default_branch_commits list."""
        skill_dir = tmp_path / "skills" / "present2"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# present2")
        db_path = _make_db(tmp_path, task_id=9997, summary="Create skills/present2/SKILL.md")
        rc, stdout, _ = _run_main(db_path, 9997)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"
        assert data["default_branch_commits"] == []
        assert data["default_branch_commit_files"] == []

    def test_direct_invocation_guard(self):
        """Direct invocation without .db first arg should exit nonzero with usage hint."""
        result = subprocess.run(
            [sys.executable, os.path.join(BIN, "tusk-check-deliverables.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "tusk wrapper" in result.stderr or "check-deliverables" in result.stderr


# ── merged_not_closed (orphaned-task case) ────────────────────────────


def _init_git_repo(repo_root, default_branch="main"):
    """Init a real git repo at repo_root with a pinned default-branch ref.

    Pins refs/remotes/origin/HEAD so _default_branch() resolves deterministically
    without relying on the host gh/git config.
    """
    subprocess.run(
        ["git", "init", "-b", default_branch, str(repo_root)],
        check=True, capture_output=True,
    )
    for k, v in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(
            ["git", "-C", str(repo_root), "config", k, v],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--allow-empty", "-m", "initial"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "symbolic-ref", "refs/remotes/origin/HEAD",
         f"refs/remotes/origin/{default_branch}"],
        check=True, capture_output=True,
    )


def _git_commit(repo_root, message):
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--allow-empty", "-m", message],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout.strip()


def _git_commit_with_files(repo_root, message, file_specs):
    """Write each (relpath, contents) in file_specs and commit them.

    Returns the new commit's SHA. Each path is repo-relative.
    """
    for relpath, contents in file_specs:
        abs_path = os.path.join(str(repo_root), relpath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(contents)
        subprocess.run(
            ["git", "-C", str(repo_root), "add", relpath],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", message],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout.strip()


class TestMergedNotClosed:
    def test_merged_not_closed_when_task_commit_on_default(self, tmp_path):
        """[TASK-N] commit on the default branch → merged_not_closed with the SHA listed."""
        _init_git_repo(tmp_path)
        sha = _git_commit(tmp_path, "[TASK-7777] orphaned implementation")
        db_path = _make_db(tmp_path, task_id=7777)
        rc, stdout, _ = _run_main(db_path, 7777)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "merged_not_closed"
        assert data["commits_found"] is True
        assert sha in data["default_branch_commits"]

    def test_commits_found_when_task_commit_on_feature_branch_only(self, tmp_path):
        """[TASK-N] commit on a non-default branch only → commits_found, not merged_not_closed."""
        _init_git_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature/TASK-8888-test"],
            check=True, capture_output=True,
        )
        _git_commit(tmp_path, "[TASK-8888] feature work")
        db_path = _make_db(tmp_path, task_id=8888)
        rc, stdout, _ = _run_main(db_path, 8888)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "commits_found"
        assert data["commits_found"] is True
        assert data["default_branch_commits"] == []


# ── merged_not_closed_low_confidence (prefix-match false-positive case) ──


class TestMergedNotClosedLowConfidence:
    """Regression tests for issue #606 — prefix-only [TASK-N] match on default
    branch must not blindly return merged_not_closed when the on-default
    commit's diff has no overlap with task scope."""

    def test_low_confidence_when_default_commit_diff_unrelated_to_task_paths(self, tmp_path):
        """Issue #606 reproducer: task description names file A; on-main [TASK-N] commit
        modifies an unrelated file B → low_confidence, not merged_not_closed."""
        _init_git_repo(tmp_path)
        sha = _git_commit_with_files(
            tmp_path,
            "[TASK-6606] unrelated cleanup",
            [("prisma/migrations/0042_cleanup.sql", "DELETE FROM scraping_url WHERE id IN (...);\n")],
        )
        db_path = _make_db(
            tmp_path,
            task_id=6606,
            summary="Fix PlaywrightBrowser deadlock",
            description="The fix lives in apps/scraper/src/laughtrack/foundation/infrastructure/http/client.py",
        )
        rc, stdout, _ = _run_main(db_path, 6606)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "merged_not_closed_low_confidence"
        assert sha in data["default_branch_commits"]
        assert "prisma/migrations/0042_cleanup.sql" in data["default_branch_commit_files"]

    def test_high_confidence_when_default_commit_diff_overlaps_task_paths(self, tmp_path):
        """Default-branch commit's changed file appears in task description → keep merged_not_closed."""
        _init_git_repo(tmp_path)
        sha = _git_commit_with_files(
            tmp_path,
            "[TASK-6607] real fix",
            [("apps/api/src/handlers/auth.py", "def authenticate(): ...\n")],
        )
        db_path = _make_db(
            tmp_path,
            task_id=6607,
            summary="Fix auth handler",
            description="Patch apps/api/src/handlers/auth.py to handle expired tokens.",
        )
        rc, stdout, _ = _run_main(db_path, 6607)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "merged_not_closed"
        assert sha in data["default_branch_commits"]
        assert "apps/api/src/handlers/auth.py" in data["default_branch_commit_files"]

    def test_high_confidence_preserved_when_no_scope_signal(self, tmp_path):
        """No task path refs and no feature-branch [TASK-N] commits → keep
        merged_not_closed (no signal is not a downgrade trigger). Preserves
        existing behavior for tasks whose descriptions don't name any files."""
        _init_git_repo(tmp_path)
        sha = _git_commit_with_files(
            tmp_path,
            "[TASK-6608] orphaned implementation",
            [("src/new_feature.py", "def feature(): ...\n")],
        )
        # summary/description name no paths and no feature branch exists
        db_path = _make_db(
            tmp_path,
            task_id=6608,
            summary="Add new feature",
            description="Implement the thing the team agreed on Tuesday.",
        )
        rc, stdout, _ = _run_main(db_path, 6608)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "merged_not_closed"
        assert sha in data["default_branch_commits"]

    def test_low_confidence_when_diff_unrelated_to_feature_branch_files(self, tmp_path):
        """Default-branch [TASK-N] commit changes file A; feature branch has [TASK-N]
        commits that change file B (the real work) → low_confidence. Mirrors
        the exact TASK-1691 incident from issue #606."""
        _init_git_repo(tmp_path)
        # The on-main false-positive commit (Prisma migration)
        default_sha = _git_commit_with_files(
            tmp_path,
            "[TASK-6609] Clean up stale scraping_url",
            [("prisma/migrations/0099_cleanup.sql", "DELETE FROM scraping_url;\n")],
        )
        # The real fix lives unmerged on a feature branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature/TASK-6609-real"],
            check=True, capture_output=True,
        )
        _git_commit_with_files(
            tmp_path,
            "[TASK-6609] real PlaywrightBrowser deadlock fix",
            [("apps/scraper/http_client.py", "class PlaywrightBrowser: ...\n")],
        )
        # Task description has no explicit paths — scope signal comes from feature branch
        db_path = _make_db(
            tmp_path,
            task_id=6609,
            summary="Fix PlaywrightBrowser deadlock",
            description="Investigation pending.",
        )
        rc, stdout, _ = _run_main(db_path, 6609)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "merged_not_closed_low_confidence"
        assert default_sha in data["default_branch_commits"]
        assert "prisma/migrations/0099_cleanup.sql" in data["default_branch_commit_files"]
