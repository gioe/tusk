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
        paths = mod._extract_paths(text)
        assert "skills/foo/SKILL.md" in paths

    def test_extracts_dotclaude_path(self):
        text = "Install to .claude/skills/bar/SKILL.md on disk"
        paths = mod._extract_paths(text)
        assert ".claude/skills/bar/SKILL.md" in paths

    def test_extracts_bin_path(self):
        text = "New script at bin/tusk-foo.py"
        paths = mod._extract_paths(text)
        assert "bin/tusk-foo.py" in paths

    def test_ignores_bare_directory(self):
        text = "Put it in skills/foo/ with no filename"
        paths = mod._extract_paths(text)
        # A bare directory (no extension) should not appear
        assert not any(p.endswith("/") and "." not in os.path.basename(p.rstrip("/")) for p in paths)

    def test_returns_empty_for_empty_string(self):
        assert mod._extract_paths("") == []

    def test_returns_empty_for_none(self):
        assert mod._extract_paths(None) == []

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
        assert set(data.keys()) == {"commits_found", "files_found", "files", "recommendation"}

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

    def test_direct_invocation_guard(self):
        """Direct invocation without .db first arg should exit nonzero with usage hint."""
        result = subprocess.run(
            [sys.executable, os.path.join(BIN, "tusk-check-deliverables.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "tusk wrapper" in result.stderr or "check-deliverables" in result.stderr
