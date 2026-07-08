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


def _make_db(
    tmp_path,
    task_id=99,
    summary="Create /foo skill",
    description="",
    criteria=None,
    scope_rows=None,
):
    """Create a minimal tasks + acceptance_criteria DB and return its path.

    DB is placed at tmp_path/tusk/tasks.db so that repo_root resolves to
    tmp_path (two dirname() calls up), matching the real tusk layout.

    Each entry in `criteria` is one of:
      - 2-tuple `(criterion, verification_spec)` — defaults
        `is_completed=0, is_deferred=0, criterion_type='manual'`
      - 4-tuple `(criterion, verification_spec, is_completed, is_deferred)` —
        `criterion_type` defaults to 'manual'
      - 5-tuple `(criterion, verification_spec, is_completed, is_deferred,
        criterion_type)` — full control (issue #806)
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
            status TEXT DEFAULT 'To Do',
            started_at TEXT
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            criterion TEXT,
            verification_spec TEXT,
            is_completed INTEGER DEFAULT 0,
            is_deferred INTEGER DEFAULT 0,
            criterion_type TEXT DEFAULT 'manual'
        );
        CREATE TABLE task_scope (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            source TEXT NOT NULL,
            reason TEXT,
            locked_at TEXT,
            locked_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
        (task_id, summary, description),
    )
    for entry in (criteria or []):
        if len(entry) == 2:
            crit, spec = entry
            is_completed, is_deferred, criterion_type = 0, 0, "manual"
        elif len(entry) == 4:
            crit, spec, is_completed, is_deferred = entry
            criterion_type = "manual"
        elif len(entry) == 5:
            crit, spec, is_completed, is_deferred, criterion_type = entry
        else:
            raise ValueError(f"criteria entry must have 2, 4, or 5 elements: {entry!r}")
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, verification_spec, "
            "is_completed, is_deferred, criterion_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, crit, spec, is_completed, is_deferred, criterion_type),
        )
    for entry in (scope_rows or []):
        if len(entry) == 2:
            pattern, source = entry
            reason = None
        elif len(entry) == 3:
            pattern, source, reason = entry
        else:
            raise ValueError(f"scope_rows entry must have 2 or 3 elements: {entry!r}")
        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source, reason) VALUES (?, ?, ?, ?)",
            (task_id, pattern, source, reason),
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

    def test_extracts_dot_directory_path(self):
        text = "Set BUNNYCDN_CDN_HOST in .github/workflows/web-ci.yml"
        paths = _extract_paths(text)
        assert ".github/workflows/web-ci.yml" in paths

    def test_extracts_dot_directory_path_with_trailing_punctuation(self):
        text = (
            "Update '.github/workflows/web-ci.yml', then verify "
            "`.github/workflows/e2e-visual.yml`."
        )
        paths = _extract_paths(text)
        assert ".github/workflows/web-ci.yml" in paths
        assert ".github/workflows/e2e-visual.yml" in paths

    def test_extracts_bin_path(self):
        text = "New script at bin/tusk-foo.py"
        paths = _extract_paths(text)
        assert "bin/tusk-foo.py" in paths

    def test_extracts_extensionless_scripts_under_known_prefixes(self):
        text = (
            "Fix bin/tusk, hooks/git/pre-commit, and scripts/bootstrap "
            "without touching bin/ or hooks/git/ as directories."
        )
        paths = _extract_paths(text)
        assert "bin/tusk" in paths
        assert "hooks/git/pre-commit" in paths
        assert "scripts/bootstrap" in paths
        assert "bin/" not in paths
        assert "hooks/git/" not in paths

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

    # ── Bare top-level deliverable filenames (issue #661) ──────────────
    # Without the whitelist, these silently dropped out of extract_paths
    # because they have no directory prefix — collapsing task-summary diff
    # stats to zero whenever a task's only deliverable was CLAUDE.md /
    # AGENTS.md / VERSION / README.md / CHANGELOG.md.

    def test_extracts_bare_claude_md(self):
        text = "Update CLAUDE.md to match the new wording."
        paths = _extract_paths(text)
        assert "CLAUDE.md" in paths

    def test_extracts_bare_agents_md(self):
        text = "Sync AGENTS.md alongside CLAUDE.md."
        paths = _extract_paths(text)
        assert "AGENTS.md" in paths
        assert "CLAUDE.md" in paths

    def test_extracts_bare_version(self):
        text = "Bump VERSION before merging."
        paths = _extract_paths(text)
        assert "VERSION" in paths

    def test_extracts_bare_readme_and_changelog(self):
        text = "See README.md and CHANGELOG.md for context."
        paths = _extract_paths(text)
        assert "README.md" in paths
        assert "CHANGELOG.md" in paths

    def test_does_not_match_versions_word(self):
        # Trailing word char must block a partial VERSION match.
        text = "We support multiple VERSIONS of the schema."
        paths = _extract_paths(text)
        assert "VERSION" not in paths

    def test_does_not_match_readme_in_longer_extension(self):
        # README.md must not match when the extension is .markdown.
        text = "Old format: README.markdown was the source of truth."
        paths = _extract_paths(text)
        assert "README.md" not in paths

    def test_does_not_match_changelog_underscore(self):
        text = "Legacy file CHANGELOG_OLD.md is archived."
        paths = _extract_paths(text)
        assert "CHANGELOG.md" not in paths

    def test_bare_filename_with_trailing_punctuation(self):
        # "edited CLAUDE.md." (sentence-ending period) should still resolve.
        text = "I just edited CLAUDE.md."
        paths = _extract_paths(text)
        assert "CLAUDE.md" in paths

    def test_bare_filename_does_not_match_in_url(self):
        # extract_paths already rejects '://' tokens; bare names inside a
        # URL path component must not slip through.
        text = "See https://example.com/CLAUDE.md for the rendered copy."
        paths = _extract_paths(text)
        assert not any("://" in p for p in paths)

    def test_failing_test_scenario_from_issue_661(self):
        # Direct mirror of the issue body's failing scenario: a task whose
        # description references both an extractable path (tests/...) and
        # bare top-level filenames must extract all three.
        text = (
            "Update CLAUDE.md and AGENTS.md per the wording fixed by "
            "tests/unit/test_typed_criteria_build.py."
        )
        paths = _extract_paths(text)
        assert "CLAUDE.md" in paths
        assert "AGENTS.md" in paths
        assert "tests/unit/test_typed_criteria_build.py" in paths

    # ── Bare top-level deliverables for non-tusk projects (issue #662) ─
    # Same root cause as #661 (the directory-prefix requirement of
    # _PATH_RE) — these are the common Python/Docker/build/metadata
    # deliverables that show up at the repo root in non-tusk projects.

    def test_extracts_bare_python_deliverables(self):
        text = "Bumped pyproject.toml and edited setup.py alongside requirements.txt."
        paths = _extract_paths(text)
        assert "pyproject.toml" in paths
        assert "setup.py" in paths
        assert "requirements.txt" in paths

    def test_extracts_bare_python_secondary_deliverables(self):
        text = "Tweaked setup.cfg, tox.ini, and MANIFEST.in for the new layout."
        paths = _extract_paths(text)
        assert "setup.cfg" in paths
        assert "tox.ini" in paths
        assert "MANIFEST.in" in paths

    def test_extracts_bare_docker_deliverables(self):
        text = "Updated Dockerfile and docker-compose.yml; added .dockerignore."
        paths = _extract_paths(text)
        assert "Dockerfile" in paths
        assert "docker-compose.yml" in paths
        assert ".dockerignore" in paths

    def test_extracts_bare_build_tooling(self):
        text = "Refactored Makefile alongside Justfile and Rakefile."
        paths = _extract_paths(text)
        assert "Makefile" in paths
        assert "Justfile" in paths
        assert "Rakefile" in paths

    def test_extracts_bare_repo_metadata(self):
        text = (
            "Replaced LICENSE, refreshed NOTICE, and updated .gitignore, "
            ".gitattributes, and .editorconfig."
        )
        paths = _extract_paths(text)
        assert "LICENSE" in paths
        assert "NOTICE" in paths
        assert ".gitignore" in paths
        assert ".gitattributes" in paths
        assert ".editorconfig" in paths

    def test_does_not_match_dockerfiles_word(self):
        # Trailing word char must block partial Dockerfile / Makefile matches.
        text = "We have multiple Dockerfiles and several Makefiles in this monorepo."
        paths = _extract_paths(text)
        assert "Dockerfile" not in paths
        assert "Makefile" not in paths

    def test_does_not_match_licensee_word(self):
        # The (?!\w) lookahead must block partial matches when the
        # whitelisted name is followed by a word char (LICENSEE,
        # NOTICED, requirements.txt2, Justfile_old).
        text = "The LICENSEE was NOTICED with requirements.txt2 and Justfile_old."
        paths = _extract_paths(text)
        assert "LICENSE" not in paths
        assert "NOTICE" not in paths
        assert "requirements.txt" not in paths
        assert "Justfile" not in paths

    def test_failing_test_scenario_from_issue_662(self):
        # Direct mirror of the issue body's failing scenario: a task whose
        # only deliverable is bare pyproject.toml at the repo root must be
        # extracted, so task-summary's file-overlap heuristic can attribute
        # the corresponding [TASK-N] commit. Pre-fix this returned [].
        text = "Add pyproject.toml at the repo root."
        paths = _extract_paths(text)
        assert "pyproject.toml" in paths

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
            "verifiable_spec_count",
            "passing_spec_count",
            "missing_creates_paths",
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

    def test_manual_pending_when_files_found_and_all_criteria_manual(self, tmp_path):
        """Issue #806: all-manual non-deferred criteria + file on disk → manual_pending.

        File existence is a noise signal when every criterion is manual —
        a referenced gitignored file (e.g. apps/web/.env.local) may exist
        regardless of whether the operator performed the external work
        (e.g. OAuth rotation in a dashboard). manual_pending blocks the
        silent auto-close path that mark_done triggers in /tusk.
        """
        present = tmp_path / "apps" / "web"
        present.mkdir(parents=True)
        (present / ".env.local").write_text("OAUTH_SECRET=stub")
        db_path = _make_db(
            tmp_path,
            task_id=8061,
            summary="Rotate OAuth secrets",
            description="Rotate values in apps/web/.env.local plus external dashboards.",
            criteria=[
                ("Update Google client secret in apps/web/.env.local", None, 0, 0, "manual"),
                ("Update Apple key ID in apps/web/.env.local", None, 0, 0, "manual"),
                ("Confirm Vercel env vars match", None, 0, 0, "manual"),
            ],
        )
        rc, stdout, _ = _run_main(db_path, 8061)
        assert rc == 0
        data = json.loads(stdout)
        assert data["files_found"] is True
        assert "apps/web/.env.local" in data["files"]
        assert data["recommendation"] == "manual_pending"

    def test_mark_done_when_files_found_and_mixed_criteria(self, tmp_path):
        """Regression for issue #806: a single non-manual criterion keeps
        the mark_done path live — file existence IS a meaningful signal
        for code/test/file-typed criteria.
        """
        present = tmp_path / "skills" / "mixed"
        present.mkdir(parents=True)
        (present / "SKILL.md").write_text("# mixed")
        db_path = _make_db(
            tmp_path,
            task_id=8062,
            summary="Create skills/mixed/SKILL.md and document it",
            criteria=[
                ("Operator confirms doc reads correctly", None, 0, 0, "manual"),
                ("skills/mixed/SKILL.md exists", None, 0, 0, "file"),
            ],
        )
        rc, stdout, _ = _run_main(db_path, 8062)
        assert rc == 0
        data = json.loads(stdout)
        assert data["files_found"] is True
        assert data["recommendation"] == "mark_done"

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


def _git_commit_with_files_at(repo_root, message, file_specs, commit_date):
    for relpath, contents in file_specs:
        abs_path = os.path.join(str(repo_root), relpath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(contents)
        subprocess.run(
            ["git", "-C", str(repo_root), "add", relpath],
            check=True, capture_output=True,
        )
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": commit_date,
        "GIT_COMMITTER_DATE": commit_date,
    }
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", message],
        check=True, capture_output=True, env=env,
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

    def test_old_task_id_commit_before_started_at_is_ignored(self, tmp_path):
        """Issue #494: after a DB reset, an older [TASK-N] commit on the
        default branch must not make the new task look already merged."""
        _init_git_repo(tmp_path)
        _git_commit_with_files_at(
            tmp_path,
            "[TASK-7] earlier incarnation",
            [("old-task-7.txt", "old\n")],
            "2026-01-15 10:00:00 +0000",
        )
        db_path = _make_db(tmp_path, task_id=7, summary="New task lifecycle")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE tasks SET started_at = '2026-04-19 10:00:00' WHERE id = 7"
        )
        conn.commit()
        conn.close()

        rc, stdout, _ = _run_main(db_path, 7)
        assert rc == 0
        data = json.loads(stdout)
        assert data["commits_found"] is False
        assert data["default_branch_commits"] == []
        assert data["recommendation"] == "implement_fresh"


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


# ── criteria_complete_no_commits (salvage / converged-work case) ──────


class TestCriteriaCompleteNoCommits:
    """Regression tests for issue #578 — when every non-deferred acceptance
    criterion is marked is_completed=1 but there are no [TASK-N] commits
    anywhere and no deliverable files on disk, the salvage / converged-work
    signal must be surfaced as a distinct recommendation."""

    def test_returns_criteria_complete_no_commits_when_all_done_and_no_commits_no_files(self, tmp_path):
        """All criteria is_completed=1, no commits, no files → criteria_complete_no_commits."""
        db_path = _make_db(
            tmp_path,
            task_id=5780,
            summary="Implement salvaged feature",
            description="No paths in description — nothing exists on disk.",
            criteria=[
                ("Step 1 done", None, 1, 0),
                ("Step 2 done", None, 1, 0),
                ("Step 3 done", None, 1, 0),
            ],
        )
        rc, stdout, _ = _run_main(db_path, 5780)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "criteria_complete_no_commits"
        assert data["commits_found"] is False
        assert data["files_found"] is False
        assert data["files"] == []
        assert data["default_branch_commits"] == []
        assert data["default_branch_commit_files"] == []

    def test_returns_implement_fresh_when_one_criterion_incomplete(self, tmp_path):
        """At least one non-deferred criterion incomplete → implement_fresh, NOT
        criteria_complete_no_commits. This is the regression guard for the
        partial-completion case."""
        db_path = _make_db(
            tmp_path,
            task_id=5781,
            summary="Implement partially-done feature",
            description="No paths in description — nothing exists on disk.",
            criteria=[
                ("Step 1 done", None, 1, 0),
                ("Step 2 still pending", None, 0, 0),
                ("Step 3 done", None, 1, 0),
            ],
        )
        rc, stdout, _ = _run_main(db_path, 5781)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "implement_fresh"

    def test_returns_implement_fresh_when_task_has_no_criteria(self, tmp_path):
        """Zero criteria → implement_fresh (no salvage signal — vacuous truth is
        not informative). Preserves existing behavior for criteria-less tasks."""
        db_path = _make_db(
            tmp_path,
            task_id=5782,
            summary="Implement criteria-less feature",
            description="No paths in description — nothing exists on disk.",
        )
        rc, stdout, _ = _run_main(db_path, 5782)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "implement_fresh"

    def test_deferred_criteria_excluded_from_check(self, tmp_path):
        """Deferred criteria (is_deferred=1) don't count toward the salvage
        signal — a task whose only completed criteria are non-deferred should
        still flip to criteria_complete_no_commits, even if a deferred one is
        incomplete."""
        db_path = _make_db(
            tmp_path,
            task_id=5783,
            summary="Implement feature with one deferred criterion",
            description="No paths in description — nothing exists on disk.",
            criteria=[
                ("Step 1 done", None, 1, 0),
                ("Step 2 done", None, 1, 0),
                ("Step 3 deferred", None, 0, 1),
            ],
        )
        rc, stdout, _ = _run_main(db_path, 5783)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "criteria_complete_no_commits"

    def test_only_deferred_criteria_returns_implement_fresh(self, tmp_path):
        """A task whose criteria are all deferred has no active criteria, so
        the salvage-signal check returns False → implement_fresh."""
        db_path = _make_db(
            tmp_path,
            task_id=5784,
            summary="Implement feature with all-deferred criteria",
            description="No paths in description — nothing exists on disk.",
            criteria=[
                ("Step 1 deferred", None, 0, 1),
                ("Step 2 deferred", None, 0, 1),
            ],
        )
        rc, stdout, _ = _run_main(db_path, 5784)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "implement_fresh"

    def test_mark_done_still_wins_when_files_found(self, tmp_path):
        """If criteria are all complete AND a deliverable file exists, the
        mark_done branch still takes precedence — files-on-disk are stronger
        evidence than is_completed flags. The criterion is declared
        `criterion_type='file'` because the verification_spec points at a
        file artifact (issue #806 only downgrades all-manual tasks)."""
        skill_dir = tmp_path / "skills" / "salvaged"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# salvaged")
        db_path = _make_db(
            tmp_path,
            task_id=5785,
            summary="Create skills/salvaged/SKILL.md",
            criteria=[("Skill exists", "skills/salvaged/SKILL.md", 1, 0, "file")],
        )
        rc, stdout, _ = _run_main(db_path, 5785)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"


class TestMarkDoneSpecGate:
    """Issue #1068: file existence is noise when the deliverable is an EDIT.

    Before recommending mark_done, the files_found branch runs the same
    incomplete code/file verification specs task-start's
    criteria_already_passing scan uses. When at least one verifiable spec
    exists and zero pass, the deliverable hasn't shipped — the referenced
    file merely predates the task — so the recommendation downgrades to
    implement_fresh. Specs use absolute paths because run_verification
    executes from its own resolved repo root, not the test tmp_path.
    """

    def _edit_task_db(self, tmp_path, task_id, spec):
        present = tmp_path / "skills" / "editme"
        present.mkdir(parents=True)
        (present / "SKILL.md").write_text("# editme\nexisting content\n")
        return _make_db(
            tmp_path,
            task_id=task_id,
            summary="Document close-sessions in skills/editme/SKILL.md",
            criteria=[
                ("SKILL.md documents close-sessions", spec, 0, 0, "code"),
            ],
        ), str(present / "SKILL.md")

    def test_edit_deliverable_downgrades_to_implement_fresh(self, tmp_path):
        db_path, skill_md = self._edit_task_db(
            tmp_path, 10681, spec=None
        )
        # Failing spec: the marker the edit would add is not in the file yet.
        spec = f"grep -q close-sessions-marker {skill_md}"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE acceptance_criteria SET verification_spec = ? WHERE task_id = 10681",
            (spec,),
        )
        conn.commit()
        conn.close()
        rc, stdout, _ = _run_main(db_path, 10681)
        assert rc == 0
        data = json.loads(stdout)
        assert data["files_found"] is True
        assert data["recommendation"] == "implement_fresh"
        assert data["verifiable_spec_count"] == 1
        assert data["passing_spec_count"] == 0

    def test_passing_spec_keeps_mark_done(self, tmp_path):
        db_path, skill_md = self._edit_task_db(tmp_path, 10682, spec=None)
        spec = f"grep -q existing {skill_md}"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE acceptance_criteria SET verification_spec = ? WHERE task_id = 10682",
            (spec,),
        )
        conn.commit()
        conn.close()
        rc, stdout, _ = _run_main(db_path, 10682)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"
        assert data["verifiable_spec_count"] == 1
        assert data["passing_spec_count"] == 1
        assert data["missing_creates_paths"] == []

    def test_missing_creates_scope_path_overrides_vacuous_passing_spec(self, tmp_path):
        # Issue #1195: a passing unrelated spec must not allow mark_done when
        # the declared creates deliverable is still absent.
        present = tmp_path / "existing_package"
        present.mkdir(parents=True)
        (present / "__init__.py").write_text("# existing\n")
        db_path = _make_db(
            tmp_path,
            task_id=11950,
            summary="Create new_module.py and refactor existing_package/__init__.py",
            criteria=[
                ("new_module.py exists", str(tmp_path / "new_module.py"), 0, 0, "file"),
                (
                    "Existing package still imports",
                    "python3 -c 'import json'",
                    0,
                    0,
                    "code",
                ),
            ],
            scope_rows=[("new_module.py", "creates")],
        )
        rc, stdout, _ = _run_main(db_path, 11950)
        assert rc == 0
        data = json.loads(stdout)
        assert data["files_found"] is True
        assert data["verifiable_spec_count"] == 2
        assert data["passing_spec_count"] == 1
        assert data["missing_creates_paths"] == ["new_module.py"]
        assert data["recommendation"] == "implement_fresh"

    def test_non_manual_without_spec_keeps_mark_done(self, tmp_path):
        # Status quo: a code-type criterion with no runnable spec leaves no
        # verifiable signal — file existence remains the deciding evidence.
        db_path, _ = self._edit_task_db(tmp_path, 10683, spec=None)
        rc, stdout, _ = _run_main(db_path, 10683)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"
        assert data["verifiable_spec_count"] == 0
        assert data["passing_spec_count"] == 0

    def test_mixed_passing_and_failing_specs_keep_mark_done(self, tmp_path):
        # One passing spec is convergence evidence even when a sibling spec
        # still fails — only the all-fail case downgrades.
        db_path, skill_md = self._edit_task_db(tmp_path, 10684, spec=None)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE acceptance_criteria SET verification_spec = ? WHERE task_id = 10684",
            (f"grep -q existing {skill_md}",),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (task_id, criterion, verification_spec, "
            "is_completed, is_deferred, criterion_type) VALUES (10684, ?, ?, 0, 0, 'code')",
            ("marker added", f"grep -q close-sessions-marker {skill_md}"),
        )
        conn.commit()
        conn.close()
        rc, stdout, _ = _run_main(db_path, 10684)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"
        assert data["verifiable_spec_count"] == 2
        assert data["passing_spec_count"] == 1

    def test_completed_criteria_specs_do_not_count(self, tmp_path):
        # A completed criterion's failing spec is irrelevant — the gate only
        # scans incomplete, non-deferred rows (mirrors task-start's scan).
        db_path, skill_md = self._edit_task_db(tmp_path, 10685, spec=None)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE acceptance_criteria SET verification_spec = ?, is_completed = 1 "
            "WHERE task_id = 10685",
            (f"grep -q close-sessions-marker {skill_md}",),
        )
        conn.commit()
        conn.close()
        rc, stdout, _ = _run_main(db_path, 10685)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"
        assert data["verifiable_spec_count"] == 0

    def test_failing_test_type_spec_downgrades_to_implement_fresh(self, tmp_path):
        # Issue #1103: the only incomplete spec is criterion_type='test' and it
        # FAILS. Before #1103 the gate excluded test-type specs, so
        # verifiable_spec_count came back 0 and the no-runnable-spec branch
        # recommended mark_done purely on file existence (concrete incident:
        # TASK-662). Now the failing test-type spec is counted and run, so the
        # recommendation downgrades to implement_fresh.
        db_path, skill_md = self._make_test_type_task(tmp_path, 10686)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE acceptance_criteria SET verification_spec = ? WHERE task_id = 10686",
            (f"grep -q close-sessions-marker {skill_md}",),
        )
        conn.commit()
        conn.close()
        rc, stdout, _ = _run_main(db_path, 10686)
        assert rc == 0
        data = json.loads(stdout)
        assert data["files_found"] is True
        assert data["recommendation"] == "implement_fresh"
        assert data["verifiable_spec_count"] == 1
        assert data["passing_spec_count"] == 0

    def test_passing_test_type_spec_keeps_mark_done(self, tmp_path):
        # A passing test-type spec is convergence evidence — file existence plus
        # a passing spec keeps mark_done (issue #1103).
        db_path, skill_md = self._make_test_type_task(tmp_path, 10687)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE acceptance_criteria SET verification_spec = ? WHERE task_id = 10687",
            (f"grep -q existing {skill_md}",),
        )
        conn.commit()
        conn.close()
        rc, stdout, _ = _run_main(db_path, 10687)
        assert rc == 0
        data = json.loads(stdout)
        assert data["recommendation"] == "mark_done"
        assert data["verifiable_spec_count"] == 1
        assert data["passing_spec_count"] == 1

    def _make_test_type_task(self, tmp_path, task_id):
        """An edit task whose only criterion is criterion_type='test'."""
        present = tmp_path / "skills" / "editme"
        present.mkdir(parents=True)
        (present / "SKILL.md").write_text("# editme\nexisting content\n")
        return _make_db(
            tmp_path,
            task_id=task_id,
            summary="Document close-sessions in skills/editme/SKILL.md",
            criteria=[
                ("Failing test passes", None, 0, 0, "test"),
            ],
        ), str(present / "SKILL.md")
