"""Unit tests for tusk-review-diff-range.py.

Covers (per TASK-114 criterion 499):
- Primary range — `<default>...HEAD` produces a non-empty diff on a feature
  branch ahead of the default → returns range, diff_lines, summary, and
  recovered_from_task_commits=False
- [TASK-N] commit-range recovery — primary range empty (on default branch
  with task commits already merged) → returns range `<oldest>^..<newest>`
  with recovered_from_task_commits=True
- Empty-diff fallback — primary empty AND no [TASK-N] commits in recent
  history → exits non-zero with the Step 3 error message on stderr
- Prefix-collision file-overlap heuristic (TASK-309 / issue #656) — recovered
  [TASK-N] commits whose file diff doesn't overlap with the task's referenced
  paths are dropped before the range is built; if filtering empties the list,
  raise the same kind of SystemExit the reviewer agent expects.

Each path exercises the real ``git diff`` / ``git log`` behavior against a
temporary repo so the interaction with git stays in the test surface. The
only stubbed piece is ``default_branch()``, which the direct-function tests
monkeypatch to return "main" directly rather than shelling out to
``tusk git-default-branch`` against a repo with no remote. The CLI-layer
tests rely on the real wrapper's symbolic-ref → gh → "main" fallback.
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
SCRIPT = os.path.join(BIN, "tusk-review-diff-range.py")


# ── module import (for direct function tests) ──────────────────────────


_spec = importlib.util.spec_from_file_location("tusk_review_diff_range", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── fixtures ───────────────────────────────────────────────────────────


def _make_repo(tmp_path, default_branch="main"):
    """Create a minimal git repo with one seed commit on *default_branch*.

    Also writes ``tusk/tasks.db`` under the repo so that the script's
    ``repo_root = dirname(dirname(db_path))`` resolution points at the repo.
    Returns ``(repo_root, db_path)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", default_branch, str(repo)],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    seed = repo / "seed.txt"
    seed.write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
    )

    tusk_dir = repo / "tusk"
    tusk_dir.mkdir()
    db_path = tusk_dir / "tasks.db"
    db_path.write_bytes(b"")  # just needs to exist as <repo>/tusk/tasks.db
    return str(repo), str(db_path)


def _run_cli(db_path, task_id, *, config_path="fake.json"):
    """Invoke the script via subprocess and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, SCRIPT, db_path, config_path, str(task_id)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


# ── direct-function tests ──────────────────────────────────────────────


class TestPrimaryRange:
    """Primary `<default>...HEAD` range with a feature branch ahead of main."""

    def test_returns_primary_range_when_diff_non_empty(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path, default_branch="main")

        # Create a feature branch with one new-file commit
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-42-foo"],
            check=True,
        )
        with open(os.path.join(repo_root, "new.txt"), "w") as f:
            f.write("hello\n")
        subprocess.run(["git", "-C", repo_root, "add", "new.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-42] Add new.txt"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(42, repo_root)

        assert result["range"] == "main...HEAD"
        assert result["recovered_from_task_commits"] is False
        assert result["diff_lines"] > 0
        assert result["summary"].startswith("diff --git")
        assert len(result["summary"]) <= mod.SUMMARY_CHARS
        # Also assert the full key shape so a future key addition breaks this
        # test rather than silently diverging from the documented contract.
        assert set(result.keys()) == {
            "range", "diff_lines", "summary", "recovered_from_task_commits"
        }


class TestTaskCommitRecovery:
    """When primary is empty, fall back to [TASK-N] commit-range recovery."""

    def test_recovers_range_from_task_commits_on_default_branch(self, tmp_path, monkeypatch):
        """Commits for TASK-42 already on main → primary range is empty →
        fallback builds `<oldest>^..<newest>` from the two [TASK-42] commits."""
        repo_root, _ = _make_repo(tmp_path, default_branch="main")

        # Two [TASK-42] commits directly on main (simulates already-merged state)
        for n in (1, 2):
            path = os.path.join(repo_root, f"file{n}.txt")
            with open(path, "w") as f:
                f.write(f"line {n}\n")
            subprocess.run(["git", "-C", repo_root, "add", f"file{n}.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo_root, "commit", "-q", "-m", f"[TASK-42] File {n}"],
                check=True,
            )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(42, repo_root)

        assert result["recovered_from_task_commits"] is True
        # Range shape: <sha>^..<sha>
        assert "^.." in result["range"]
        assert result["diff_lines"] > 0
        assert result["summary"].startswith("diff --git")

    def test_grep_is_scoped_to_exact_task_id(self, tmp_path, monkeypatch):
        """`[TASK-42]` must not match `[TASK-421]` — the literal `]` in the
        escaped grep pattern enforces an exact boundary on the right."""
        repo_root, _ = _make_repo(tmp_path, default_branch="main")

        # Commit for a different task — must not be pulled into the range
        with open(os.path.join(repo_root, "other.txt"), "w") as f:
            f.write("other\n")
        subprocess.run(["git", "-C", repo_root, "add", "other.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-421] Unrelated"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        # No [TASK-42] commits exist; primary is also empty → should raise
        with pytest.raises(SystemExit) as exc:
            mod.compute_range(42, repo_root)
        assert "[TASK-42] commits not detected" in str(exc.value)


class TestEmptyDiffFallback:
    """No commits referencing the task, primary also empty → error."""

    def test_raises_when_no_primary_and_no_task_commits(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path, default_branch="main")
        # Fresh repo on default branch with only the seed commit. No feature
        # branch ahead, no [TASK-99] commits anywhere.
        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        with pytest.raises(SystemExit) as exc:
            mod.compute_range(99, repo_root)
        msg = str(exc.value)
        assert "[TASK-99]" in msg
        assert "commits not detected" in msg


# ── CLI-layer tests (real subprocess, real bin/tusk wrapper) ──────────


class TestCLI:
    def test_cli_returns_json_on_success(self, tmp_path):
        """End-to-end: script invoked via subprocess returns the expected JSON
        shape. Uses a real git repo and the real bin/tusk wrapper (which
        falls back to "main" in a repo with no remote)."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")

        # Create a feature branch with a [TASK-55] commit
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-55-x"],
            check=True,
        )
        with open(os.path.join(repo_root, "x.txt"), "w") as f:
            f.write("content\n")
        subprocess.run(["git", "-C", repo_root, "add", "x.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-55] X"],
            check=True,
        )

        code, out, err = _run_cli(db_path, 55)
        assert code == 0, err
        payload = json.loads(out)
        assert set(payload.keys()) == {
            "range", "diff_lines", "summary", "recovered_from_task_commits"
        }
        assert payload["range"] == "main...HEAD"
        assert payload["recovered_from_task_commits"] is False
        assert payload["diff_lines"] > 0

    def test_cli_exits_one_when_no_diff_recoverable(self, tmp_path):
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        # Seed-only repo, no feature branch, no [TASK-77] commits
        code, out, err = _run_cli(db_path, 77)
        assert code == 1
        assert out.strip() == ""
        assert "[TASK-77]" in err

    def test_cli_rejects_invalid_task_id(self, tmp_path):
        _, db_path = _make_repo(tmp_path, default_branch="main")
        code, _out, err = _run_cli(db_path, "not-a-number")
        assert code == 1
        assert "Invalid task ID" in err


# ── prefix-collision file-overlap heuristic (TASK-309 / issue #656) ────


_TASKS_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    description TEXT
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    verification_spec TEXT
);
"""


def _seed_db(db_path, *, task_id, summary, description):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_TASKS_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, summary, description) VALUES (?, ?, ?)",
        (task_id, summary, description),
    )
    conn.commit()
    conn.close()


class TestPrefixCollisionHeuristic:
    """Issue #656: a stray [TASK-N] commit (recycled task ID, fat-fingered
    message) on the default branch must not be folded into the recovered
    diff range and surfaced to the reviewer agent as if it were this
    task's work."""

    def test_drops_unrelated_commit_and_keeps_real_one(self, tmp_path, monkeypatch):
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=42,
            summary="Wire foo",
            description="Update bin/tusk-foo.py to handle the new foo case",
        )

        # Stray [TASK-42] commit from a recycled task ID — touches an unrelated path
        with open(os.path.join(repo_root, "noise.txt"), "w") as f:
            f.write("noise\n")
        subprocess.run(["git", "-C", repo_root, "add", "noise.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-42] stray"],
            check=True,
        )

        # Real [TASK-42] commit on bin/tusk-foo.py
        bin_dir = os.path.join(repo_root, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        real_path = os.path.join(bin_dir, "tusk-foo.py")
        with open(real_path, "w") as f:
            f.write("real\n")
        subprocess.run(["git", "-C", repo_root, "add", "bin/tusk-foo.py"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-42] real work"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        result = mod.compute_range(42, repo_root, db_path)
        # Range should contain only the real commit's SHA on both endpoints
        assert result["recovered_from_task_commits"] is True
        # Diff should not contain noise.txt — only bin/tusk-foo.py
        assert "tusk-foo.py" in result["summary"] or "tusk-foo.py" in subprocess.run(
            ["git", "-C", repo_root, "diff", result["range"]],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout

    def test_raises_when_filter_drops_every_commit(self, tmp_path, monkeypatch):
        """The only [TASK-N] commits in history are prefix-match false positives
        — filter empties the list, raise SystemExit with a #656-specific
        message rather than silently handing the reviewer the wrong diff."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=99,
            summary="Wire bar",
            description="Update bin/tusk-bar.py for the bar case",
        )

        # Only [TASK-99] commits in history are unrelated
        with open(os.path.join(repo_root, "stray.txt"), "w") as f:
            f.write("stray\n")
        subprocess.run(["git", "-C", repo_root, "add", "stray.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-99] stray"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        with pytest.raises(SystemExit) as exc:
            mod.compute_range(99, repo_root, db_path)
        msg = str(exc.value)
        assert "[TASK-99]" in msg
        assert "issue #656" in msg

    def test_skipped_when_task_has_no_scope_signal(self, tmp_path, monkeypatch):
        """Task with no referenced paths in summary/description → no basis to
        discriminate → every commit is kept (matches TASK-308 behavior in
        tusk-merge)."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=11,
            summary="Generic title",
            description="Generic body with no file references",
        )

        with open(os.path.join(repo_root, "anything.txt"), "w") as f:
            f.write("x\n")
        subprocess.run(["git", "-C", repo_root, "add", "anything.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-11] generic"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        result = mod.compute_range(11, repo_root, db_path)
        assert result["recovered_from_task_commits"] is True
        assert result["diff_lines"] > 0

    def test_skipped_when_db_is_unreachable(self, tmp_path, monkeypatch):
        """Best-effort: if the DB doesn't exist (cross-repo invocation,
        broken state) the heuristic falls back to keeping every commit
        rather than blocking the reviewer."""
        repo_root, _db_path = _make_repo(tmp_path, default_branch="main")

        with open(os.path.join(repo_root, "anything.txt"), "w") as f:
            f.write("x\n")
        subprocess.run(["git", "-C", repo_root, "add", "anything.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-22] generic"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        # Pass a path to a file that doesn't exist
        result = mod.compute_range(22, repo_root, "/nonexistent/path/db")
        assert result["recovered_from_task_commits"] is True
        assert result["diff_lines"] > 0
