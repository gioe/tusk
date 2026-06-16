"""Unit tests for task-start's convergence-recency hint (issue #1048).

Builds a real git repo plus a minimal tasks/acceptance_criteria DB and drives
_convergence_recency_hint directly, asserting the stderr hint fires only when an
other-task commit touching a cited file landed within the window.
"""

import importlib.util
import os
import sqlite3
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
START_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-start.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_task_start", START_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture()
def repo(tmp_path):
    """A git repo with src/foo/ShowRow.py last touched by a [TASK-999] commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(["init", "-b", "main", str(repo_dir)], str(tmp_path))
    _git(["config", "user.email", "t@t.t"], str(repo_dir))
    _git(["config", "user.name", "t"], str(repo_dir))
    sub = repo_dir / "src" / "foo"
    sub.mkdir(parents=True)
    (sub / "ShowRow.py").write_text("x = 1\n")
    (repo_dir / "unrelated.py").write_text("y = 2\n")
    _git(["add", "."], str(repo_dir))
    _git(["commit", "-m", "[TASK-999] fix ShowRow rendering"], str(repo_dir))
    return str(repo_dir)


def _make_db(tmp_path, *, description, created_at_sql="datetime('now')"):
    db = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            summary TEXT,
            description TEXT,
            created_at TEXT
        );
        CREATE TABLE acceptance_criteria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            criterion TEXT,
            verification_spec TEXT
        );
        """
    )
    conn.execute(
        f"INSERT INTO tasks (id, summary, description, created_at) "
        f"VALUES (2746, 'fix swift suites', ?, {created_at_sql})",
        (description,),
    )
    conn.commit()
    return conn


def test_hint_fires_for_other_task_commit_on_cited_path(repo, tmp_path, capsys):
    mod = _load_module()
    conn = _make_db(tmp_path, description="The failure is in src/foo/ShowRow.py")
    mod._convergence_recency_hint(conn, 2746, repo)
    err = capsys.readouterr().err
    assert "possible convergence" in err
    assert "TASK-999" in err


def test_hint_fires_for_bare_basename_citation(repo, tmp_path, capsys):
    mod = _load_module()
    # Description cites the bare filename (ShowRow.py:12 style), no directory —
    # exercises the basename pathspec branch.
    conn = _make_db(tmp_path, description="See ShowRow.py:12 for the broken row")
    mod._convergence_recency_hint(conn, 2746, repo)
    err = capsys.readouterr().err
    assert "possible convergence" in err
    assert "TASK-999" in err


def test_no_hint_when_no_paths_cited(repo, tmp_path, capsys):
    mod = _load_module()
    conn = _make_db(tmp_path, description="Something vague with no file references")
    mod._convergence_recency_hint(conn, 2746, repo)
    assert "possible convergence" not in capsys.readouterr().err


def test_no_hint_when_commit_out_of_window(repo, tmp_path, capsys):
    mod = _load_module()
    # created_at 30 days in the future → since = created_at-7d is well after the
    # just-made commit → excluded.
    conn = _make_db(
        tmp_path,
        description="The failure is in src/foo/ShowRow.py",
        created_at_sql="datetime('now', '+30 days')",
    )
    mod._convergence_recency_hint(conn, 2746, repo)
    assert "possible convergence" not in capsys.readouterr().err


def test_no_hint_for_unrelated_file(repo, tmp_path, capsys):
    mod = _load_module()
    # Cited file exists but was not touched by the [TASK-999] commit's path set
    # in a way that overlaps — cite a path no commit touched.
    conn = _make_db(tmp_path, description="The failure is in src/foo/Missing.py")
    mod._convergence_recency_hint(conn, 2746, repo)
    assert "possible convergence" not in capsys.readouterr().err


def test_no_hint_when_only_self_task_referenced(repo, tmp_path, capsys):
    mod = _load_module()
    # A commit referencing THIS task (2746) on the cited path must be filtered.
    sub = os.path.join(repo, "src", "foo")
    with open(os.path.join(sub, "ShowRow.py"), "w", encoding="utf-8") as f:
        f.write("x = 2\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "[TASK-2746] self work on ShowRow"], repo)
    conn = _make_db(tmp_path, description="The failure is in src/foo/ShowRow.py")
    mod._convergence_recency_hint(conn, 2746, repo)
    err = capsys.readouterr().err
    # The other-task (999) commit is still in history, so the hint DOES fire,
    # but it must reference 999 and never 2746 as a convergence hit.
    assert "TASK-999" in err
    # The self commit subject must not be listed as a convergence hit line.
    assert "self work on ShowRow" not in err


def test_no_repo_root_is_noop(tmp_path, capsys):
    mod = _load_module()
    conn = _make_db(tmp_path, description="src/foo/ShowRow.py")
    mod._convergence_recency_hint(conn, 2746, None)
    assert capsys.readouterr().err == ""


# --- issue #1104: noise suppression on high-churn paths ----------------------


def test_no_hint_when_only_bookkeeping_files_cited(repo, tmp_path, capsys):
    """Citing only VERSION / CHANGELOG.md must empty the pathspec set so the
    hint never matches the universal version-bump churn (issue #1104)."""
    mod = _load_module()
    # A [TASK-998] commit touches VERSION — exactly the kind of bump churn the
    # old hint over-matched on.
    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("5\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "[TASK-998] Bump VERSION to 5 and update CHANGELOG"], repo)
    conn = _make_db(
        tmp_path,
        description="Bump the VERSION file and update CHANGELOG.md every release",
    )
    mod._convergence_recency_hint(conn, 2746, repo)
    assert "possible convergence" not in capsys.readouterr().err


def test_version_bump_commits_are_filtered_from_hits(repo, tmp_path, capsys):
    """A commit that touches a genuinely cited file but whose subject is a pure
    version bump must be dropped from the surfaced list (issue #1104)."""
    mod = _load_module()
    # Other.py is touched ONLY by a bump-subject commit referencing another task.
    sub = os.path.join(repo, "src", "foo")
    with open(os.path.join(sub, "Other.py"), "w", encoding="utf-8") as f:
        f.write("z = 3\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "[TASK-998] Bump VERSION to 6 and update CHANGELOG"], repo)
    conn = _make_db(tmp_path, description="The failure is in src/foo/Other.py")
    mod._convergence_recency_hint(conn, 2746, repo)
    err = capsys.readouterr().err
    assert "possible convergence" not in err
    assert "TASK-998" not in err


def test_true_positive_survives_alongside_bookkeeping_citation(repo, tmp_path, capsys):
    """A real convergent commit still fires even when the description also cites
    bookkeeping files and a sibling bump commit exists (issue #1104)."""
    mod = _load_module()
    # A bump commit touching VERSION referencing TASK-998 (noise) plus the base
    # repo's real [TASK-999] commit on ShowRow.py (signal).
    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("7\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "[TASK-998] Bump VERSION to 7 and update CHANGELOG"], repo)
    conn = _make_db(
        tmp_path,
        description="The failure is in src/foo/ShowRow.py; remember to bump VERSION",
    )
    mod._convergence_recency_hint(conn, 2746, repo)
    err = capsys.readouterr().err
    assert "possible convergence" in err
    assert "TASK-999" in err
    assert "TASK-998" not in err


def test_surfaced_count_matches_shown_and_is_capped(repo, tmp_path, capsys):
    """With more convergent commits than the cap, the warning reports the shown
    count as 'N of M' and prints exactly N hit lines (issue #1104)."""
    mod = _load_module()
    sub = os.path.join(repo, "src", "foo")
    # 12 additional non-bump commits, each touching the cited file and naming a
    # distinct sibling task. Plus the base [TASK-999] commit = 13 hits total.
    for i in range(12):
        with open(os.path.join(sub, "ShowRow.py"), "w", encoding="utf-8") as f:
            f.write(f"x = {i + 10}\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", f"[TASK-{1001 + i}] tweak ShowRow {i}"], repo)
    conn = _make_db(tmp_path, description="The failure is in src/foo/ShowRow.py")
    mod._convergence_recency_hint(conn, 2746, repo)
    err = capsys.readouterr().err
    assert "possible convergence" in err
    assert "10 of 13 commit(s)" in err
    hit_lines = [ln for ln in err.splitlines() if ln.startswith("  ")]
    assert len(hit_lines) == 10
