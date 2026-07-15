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


def _run_cli(db_path, task_id, *, config_path="fake.json", cwd=None):
    """Invoke the script via subprocess and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, SCRIPT, db_path, config_path, str(task_id)]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
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

        head_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert result["range"] == f"main...{head_sha}"
        assert result["recovered_from_task_commits"] is False
        assert result["diff_lines"] > 0
        assert result["summary"].startswith("diff --git")
        assert len(result["summary"]) <= mod.SUMMARY_CHARS
        # diff_lines_meaningful subtracts auto-generated lockfile sections
        # (issue #761). With no lockfiles in the diff, it equals diff_lines.
        assert result["diff_lines_meaningful"] == result["diff_lines"]
        # Also assert the full key shape so a future key addition breaks this
        # test rather than silently diverging from the documented contract.
        assert set(result.keys()) == {
            "range",
            "diff_lines",
            "diff_lines_meaningful",
            "summary",
            "recovered_from_task_commits",
            "resolved_repo_root",
        }
        # TASK-412: the resolved checkout is the one we invoked against.
        assert os.path.realpath(result["resolved_repo_root"]) == os.path.realpath(repo_root)

    def test_uses_origin_default_when_local_default_missing(self, tmp_path, monkeypatch):
        """Issue #696: linked worktrees can have a usable origin/main while the
        local default ref is missing or stale. In that shape, prefer the
        remote-tracking default range before falling back to [TASK-N] commits."""
        repo_root, _ = _make_repo(tmp_path, default_branch="main")
        subprocess.run(
            ["git", "-C", repo_root, "update-ref", "refs/remotes/origin/main", "HEAD"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-42-foo"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_root, "branch", "-D", "main"],
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

        head_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert result["range"] == f"origin/main...{head_sha}"
        assert result["recovered_from_task_commits"] is False
        assert result["diff_lines"] > 0

    def test_uses_origin_default_when_local_default_is_stale(self, tmp_path, monkeypatch):
        """Issue #699: a stale but valid local default branch must not inflate
        review diffs with commits that already exist on the remote default."""
        repo_root, _ = _make_repo(tmp_path, default_branch="main")
        seed_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()

        with open(os.path.join(repo_root, "release.txt"), "w") as f:
            f.write("release\n")
        subprocess.run(["git", "-C", repo_root, "add", "release.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "Release already on origin"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_root, "update-ref", "refs/remotes/origin/main", "HEAD"],
            check=True,
        )
        subprocess.run(["git", "-C", repo_root, "reset", "--hard", seed_sha], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-42-foo", "origin/main"],
            check=True,
        )

        with open(os.path.join(repo_root, "task.txt"), "w") as f:
            f.write("task\n")
        subprocess.run(["git", "-C", repo_root, "add", "task.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-42] Task change"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(42, repo_root)
        remote_diff = subprocess.run(
            ["git", "-C", repo_root, "diff", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout

        head_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert result["range"] == f"origin/main...{head_sha}"
        assert result["recovered_from_task_commits"] is False
        assert result["diff_lines"] == remote_diff.count("\n")
        assert "task.txt" in result["summary"]
        assert "release.txt" not in subprocess.run(
            ["git", "-C", repo_root, "diff", result["range"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout

    def test_uses_origin_default_when_local_default_has_diverged(self, tmp_path, monkeypatch):
        """A checked-out primary worktree can leave local main diverged from
        origin/main while task worktrees continue from origin/main. Prefer the
        remote default to keep review diffs scoped to task work."""
        repo_root, _ = _make_repo(tmp_path, default_branch="main")
        seed_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()

        with open(os.path.join(repo_root, "remote.txt"), "w") as f:
            f.write("remote\n")
        subprocess.run(["git", "-C", repo_root, "add", "remote.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "Remote default"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_root, "update-ref", "refs/remotes/origin/main", "HEAD"],
            check=True,
        )

        subprocess.run(["git", "-C", repo_root, "reset", "--hard", seed_sha], check=True)
        with open(os.path.join(repo_root, "local.txt"), "w") as f:
            f.write("local\n")
        subprocess.run(["git", "-C", repo_root, "add", "local.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "Local divergent default"],
            check=True,
        )

        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-43-foo", "origin/main"],
            check=True,
        )
        with open(os.path.join(repo_root, "task.txt"), "w") as f:
            f.write("task\n")
        subprocess.run(["git", "-C", repo_root, "add", "task.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-43] Task change"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(43, repo_root)

        head_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert result["range"] == f"origin/main...{head_sha}"
        assert "task.txt" in result["summary"]
        assert "local.txt" not in subprocess.run(
            ["git", "-C", repo_root, "diff", result["range"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout

    def test_uses_local_default_when_unpublished_commits_are_task_ancestors(
        self, tmp_path, monkeypatch
    ):
        """Issue #1209: a rebase onto unpublished local main must not make
        those inherited commits passengers in the task review.
        """
        repo_root, _ = _make_repo(tmp_path, default_branch="main")
        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        seed_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git", "-C", repo_root, "update-ref",
                "refs/remotes/origin/main", seed_sha,
            ],
            check=True,
        )

        with open(os.path.join(repo_root, "local-only.txt"), "w") as f:
            f.write("local-only\n")
        subprocess.run(
            ["git", "-C", repo_root, "add", "local-only.txt"], check=True
        )
        subprocess.run(
            [
                "git", "-C", repo_root, "commit", "-q", "-m",
                "Local unpublished work",
            ],
            check=True,
        )

        # Start from the recorded origin baseline, then reproduce the incident
        # by rebasing the task commit onto the unpublished local default.
        subprocess.run(
            [
                "git", "-C", repo_root, "checkout", "-q", "-b",
                "feature/TASK-44-rebased", "origin/main",
            ],
            check=True,
        )
        with open(os.path.join(repo_root, "task-only.txt"), "w") as f:
            f.write("task-only\n")
        subprocess.run(
            ["git", "-C", repo_root, "add", "task-only.txt"], check=True
        )
        subprocess.run(
            [
                "git", "-C", repo_root, "commit", "-q", "-m",
                "[TASK-44] Task work",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_root, "rebase", "main"], check=True
        )

        head_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        result = mod.compute_range(44, repo_root)

        assert result["range"] == f"main...{head_sha}"
        diff = subprocess.run(
            ["git", "-C", repo_root, "diff", result["range"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        assert "task-only" in diff
        assert "local-only" not in diff
        assert result["diff_lines"] == diff.count("\n")


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
            "range",
            "diff_lines",
            "diff_lines_meaningful",
            "summary",
            "recovered_from_task_commits",
            "resolved_repo_root",
        }
        head_sha = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert payload["range"] == f"main...{head_sha}"
        assert payload["recovered_from_task_commits"] is False
        assert os.path.realpath(payload["resolved_repo_root"]) == os.path.realpath(repo_root)
        # No lockfiles in this diff → meaningful count equals diff_lines.
        assert payload["diff_lines_meaningful"] == payload["diff_lines"]
        assert payload["diff_lines"] > 0

    def test_cli_uses_invocation_worktree_instead_of_primary_db_checkout(self, tmp_path):
        """Issue #686: when invoked from a linked worktree, use that checkout's
        HEAD even though the DB path still lives under the primary checkout."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        worktree = tmp_path / "linked"
        subprocess.run(
            [
                "git", "-C", repo_root, "worktree", "add", "-q",
                "-b", "feature/TASK-56-linked", str(worktree), "HEAD",
            ],
            check=True,
        )
        with open(worktree / "linked.txt", "w", encoding="utf-8") as f:
            f.write("linked\n")
        subprocess.run(["git", "-C", str(worktree), "add", "linked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-q", "-m", "[TASK-56] Linked work"],
            check=True,
        )

        code, out, err = _run_cli(db_path, 56, cwd=worktree)

        assert code == 0, err
        payload = json.loads(out)
        head_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert payload["range"] == f"main...{head_sha}"
        assert payload["recovered_from_task_commits"] is False
        assert payload["diff_lines"] > 0
        assert "linked.txt" in payload["summary"]

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

    def test_cli_uses_invocation_worktree_not_db_checkout(self, tmp_path):
        """Issue #730: a task worktree must review its own HEAD even when
        the DB lives under the main checkout and that checkout is on another
        feature branch."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        subprocess.run(
            ["git", "-C", repo_root, "update-ref", "refs/remotes/origin/main", "HEAD"],
            check=True,
        )
        worktree = tmp_path / "TASK-42-worktree"
        subprocess.run(
            [
                "git", "-C", repo_root, "worktree", "add", "-q", "-b",
                "feature/TASK-42-worktree", str(worktree), "origin/main",
            ],
            check=True,
        )
        task_file = worktree / "task-worktree.txt"
        task_file.write_text("task worktree\n")
        subprocess.run(["git", "-C", str(worktree), "add", "task-worktree.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-q", "-m", "[TASK-42] worktree change"],
            check=True,
        )

        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-99-other"],
            check=True,
        )
        other_file = os.path.join(repo_root, "other-checkout.txt")
        with open(other_file, "w", encoding="utf-8") as f:
            f.write("other checkout\n")
        subprocess.run(["git", "-C", repo_root, "add", "other-checkout.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-99] other change"],
            check=True,
        )

        code, out, err = _run_cli(db_path, 42, cwd=str(worktree))
        assert code == 0, err
        payload = json.loads(out)
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", payload["range"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout

        head_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        assert payload["range"] == f"origin/main...{head_sha}"
        assert "task-worktree.txt" in payload["summary"]
        assert "other-checkout.txt" not in payload["summary"]
        assert "task-worktree.txt" in diff
        assert "other-checkout.txt" not in diff


# ── prefix-collision file-overlap heuristic (TASK-309 / issue #656) ────


_TASKS_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    description TEXT,
    started_at TEXT
);
CREATE TABLE acceptance_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    criterion TEXT,
    verification_spec TEXT
);
"""


def _seed_db(db_path, *, task_id, summary, description, started_at=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_TASKS_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, summary, description, started_at) VALUES (?, ?, ?, ?)",
        (task_id, summary, description, started_at),
    )
    conn.commit()
    conn.close()


class TestPrefixCollisionHeuristic:
    """Issue #656: a stray [TASK-N] commit (recycled task ID, fat-fingered
    message) on the default branch must not be folded into the recovered
    diff range and surfaced to the reviewer agent as if it were this
    task's work."""

    def test_drops_unrelated_commit_and_keeps_real_one(self, tmp_path, monkeypatch):
        """A stray [TASK-N] commit on a side branch with no parent-link to
        the real commit's chain must be dropped by the block-level scope
        filter, leaving only the real commit in the recovered range.

        Issue #856: the previous version of this test put stray and real on
        the same linear chain on main. After TASK-434's block-level filter,
        contiguous strays form one block with the real commit and both are
        kept under the new policy — and this test's only assertion (that
        tusk-foo.py is somewhere in the diff) silently passed even when the
        stray's file was also present, defeating the stated drop semantic.
        The non-contiguous layout (stray on a side branch unreachable from
        the real commit's parent chain) is the canonical regression vector
        — the two commits form separate blocks, only the real block's
        files overlap task scope, so the stray block is dropped.
        """
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=42,
            summary="Wire foo",
            description="Update bin/tusk-foo.py to handle the new foo case",
        )

        # Stray [TASK-42] commit on a side branch unreachable from main's chain.
        # Under the block-level filter, its parent (seed) is not in the matched
        # set, so the stray forms a block by itself with files = {noise.txt}.
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "side"],
            check=True,
        )
        with open(os.path.join(repo_root, "noise.txt"), "w") as f:
            f.write("noise\n")
        subprocess.run(["git", "-C", repo_root, "add", "noise.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-42] stray"],
            check=True,
        )

        # Real [TASK-42] commit on main (non-contiguous with the side branch).
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "main"],
            check=True,
        )
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
        assert result["recovered_from_task_commits"] is True

        recovered_diff = subprocess.run(
            ["git", "-C", repo_root, "diff", result["range"]],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout

        # Real commit's file IS in the diff.
        assert "tusk-foo.py" in result["summary"] or "tusk-foo.py" in recovered_diff
        # Stray commit's file IS NOT in the diff — the prefix-collision drop
        # semantic the docstring describes.
        assert "noise.txt" not in result["summary"], (
            f"noise.txt must not be in result['summary']; got {result['summary']!r}"
        )
        assert "noise.txt" not in recovered_diff, (
            f"noise.txt must not be in the recovered diff; got:\n{recovered_diff}"
        )

    def test_falls_through_when_no_block_overlaps_scope_signal(
        self, tmp_path, monkeypatch
    ):
        """Extraction-miss fall-through (mirrors issue #851 from
        tusk-task-summary): when no block intersects the task's scope
        signal, return every commit unchanged rather than refusing.
        The signal is more likely off-scope (a precedent citation in
        the description) than every matched commit being a recycled-ID
        stray; over-inclusion is recoverable, silent zero-range refusal
        is not. Pre-#842 behavior was to raise the #656 prefix-collision
        SystemExit here; the fallthrough subsumes that refusal."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=99,
            summary="Wire bar",
            description="Update bin/tusk-bar.py for the bar case",
        )

        # Only [TASK-99] commit in history touches a file that's outside
        # the scope signal (bin/tusk-bar.py). With the block-level
        # filter and fallthrough, the commit is kept rather than dropped.
        with open(os.path.join(repo_root, "stray.txt"), "w") as f:
            f.write("stray\n")
        subprocess.run(["git", "-C", repo_root, "add", "stray.txt"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-99] stray"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        result = mod.compute_range(99, repo_root, db_path)
        assert result["recovered_from_task_commits"] is True
        assert result["diff_lines"] > 0
        assert "stray.txt" in result["summary"]

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

    def test_block_keeps_sibling_commits_842(self, tmp_path, monkeypatch):
        """Issue #842 regression: when a contiguous run of [TASK-N]
        commits includes both an in-scope source commit AND off-scope
        sibling commits (test, VERSION bump, CHANGELOG), the block-level
        filter keeps the whole block so the recovered range covers all
        commits — not just the single source commit.

        Mirrors TASK-421's incident: 3 commits where only the source
        commit touches a referenced path; under the old per-commit
        filter the recovered range was <src>^..<src> covering 1 commit;
        under the block-level filter it covers all 3."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=421,
            summary="Fix abandon cwd",
            description="Update bin/tusk-abandon.py to handle CWD correctly",
        )

        # Commit 1: source fix touching the referenced path.
        os.makedirs(os.path.join(repo_root, "bin"), exist_ok=True)
        with open(os.path.join(repo_root, "bin", "tusk-abandon.py"), "w") as f:
            f.write("source fix\n")
        subprocess.run(
            ["git", "-C", repo_root, "add", "bin/tusk-abandon.py"], check=True
        )
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-421] fix"],
            check=True,
        )

        # Commit 2: sibling test commit touching only a new test file.
        os.makedirs(os.path.join(repo_root, "tests", "integration"), exist_ok=True)
        with open(
            os.path.join(repo_root, "tests", "integration", "test_abandon.py"), "w"
        ) as f:
            f.write("test\n")
        subprocess.run(
            ["git", "-C", repo_root, "add", "tests/integration/test_abandon.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-421] test"],
            check=True,
        )

        # Commit 3: sibling VERSION bump touching only VERSION + CHANGELOG.
        with open(os.path.join(repo_root, "VERSION"), "w") as f:
            f.write("999\n")
        with open(os.path.join(repo_root, "CHANGELOG.md"), "w") as f:
            f.write("# Changelog\n")
        subprocess.run(
            ["git", "-C", repo_root, "add", "VERSION", "CHANGELOG.md"], check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "commit",
                "-q",
                "-m",
                "[TASK-421] Bump VERSION to 999 and update CHANGELOG",
            ],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        result = mod.compute_range(421, repo_root, db_path)
        assert result["recovered_from_task_commits"] is True

        # The recovered range must cover all 3 commits, not just the source.
        rev_list = subprocess.run(
            ["git", "-C", repo_root, "rev-list", result["range"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        covered = [line for line in rev_list.stdout.splitlines() if line.strip()]
        assert len(covered) == 3, (
            f"expected 3 commits covered, got {len(covered)}: range={result['range']!r}"
        )

        diff = subprocess.run(
            ["git", "-C", repo_root, "diff", result["range"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        assert "bin/tusk-abandon.py" in diff
        assert "tests/integration/test_abandon.py" in diff
        assert "VERSION" in diff
        assert "CHANGELOG.md" in diff


class TestDiffLinesMeaningful:
    """Issue #761: ``diff_lines_meaningful`` subtracts auto-generated lockfile
    sections so the inline-vs-agent routing threshold tracks human-readable
    change size rather than raw newline count.

    The primary range and the [TASK-N] commit-recovery range must both
    emit the field; lockfile-only diffs should report
    ``diff_lines_meaningful == 0`` while ``diff_lines`` is non-zero."""

    def test_primary_range_subtracts_package_lock(self, tmp_path, monkeypatch):
        repo_root, _db_path = _make_repo(tmp_path, default_branch="main")
        # Feature branch: one src/foo.py change + a big package-lock.json change
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-77-x"],
            check=True,
        )
        os.makedirs(os.path.join(repo_root, "src"), exist_ok=True)
        with open(os.path.join(repo_root, "src", "foo.py"), "w") as f:
            f.write("def foo():\n    return 1\n")
        lockfile_body = "{\n" + "  " + ",\n  ".join(f'"k{i}": {i}' for i in range(60)) + "\n}\n"
        with open(os.path.join(repo_root, "package-lock.json"), "w") as f:
            f.write(lockfile_body)
        subprocess.run(["git", "-C", repo_root, "add", "src/foo.py", "package-lock.json"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-77] feature + lockfile"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(77, repo_root, db_path=None)

        assert result["recovered_from_task_commits"] is False
        assert result["diff_lines"] > 0
        assert "diff_lines_meaningful" in result
        # Lockfile sections were subtracted → meaningful count is strictly less.
        assert result["diff_lines_meaningful"] < result["diff_lines"]

    def test_lockfile_only_diff_meaningful_is_zero(self, tmp_path, monkeypatch):
        repo_root, _db_path = _make_repo(tmp_path, default_branch="main")
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-78-locks"],
            check=True,
        )
        # Only a lockfile change.
        lockfile_body = "{\n" + ",\n".join(f'  "k{i}": {i}' for i in range(20)) + "\n}\n"
        with open(os.path.join(repo_root, "yarn.lock"), "w") as f:
            f.write(lockfile_body)
        subprocess.run(["git", "-C", repo_root, "add", "yarn.lock"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-78] yarn"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(78, repo_root, db_path=None)

        assert result["diff_lines"] > 0
        assert result["diff_lines_meaningful"] == 0

    def test_no_lockfiles_meaningful_equals_diff_lines(self, tmp_path, monkeypatch):
        repo_root, _db_path = _make_repo(tmp_path, default_branch="main")
        subprocess.run(
            ["git", "-C", repo_root, "checkout", "-q", "-b", "feature/TASK-79-src"],
            check=True,
        )
        with open(os.path.join(repo_root, "a.py"), "w") as f:
            f.write("a = 1\n")
        subprocess.run(["git", "-C", repo_root, "add", "a.py"], check=True)
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-q", "-m", "[TASK-79] src"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")
        result = mod.compute_range(79, repo_root, db_path=None)

        assert result["diff_lines"] > 0
        assert result["diff_lines_meaningful"] == result["diff_lines"]


class TestWorktreeFallback:
    """Issue #777: ``tusk review begin`` invoked from a checkout that does
    not carry the feature branch's commits should consult ``git worktree
    list`` for a sibling worktree on ``feature/TASK-<id>-*`` and re-run
    against it, instead of failing with a generic "No changes found".

    These tests build a primary checkout plus a sibling worktree carrying
    the [TASK-N] commit and assert that ``compute_range`` invoked against
    the primary returns the sibling's payload."""

    def _add_sibling_worktree(self, primary, task_id):
        """Create a sibling worktree carrying a [TASK-N] commit on
        ``feature/TASK-<id>-x``. Returns the worktree path."""
        sibling = primary + f"-wt-{task_id}"
        branch = f"feature/TASK-{task_id}-x"
        subprocess.run(
            ["git", "-C", primary, "worktree", "add", "-b", branch, sibling],
            check=True, capture_output=True,
        )
        with open(os.path.join(sibling, "real.py"), "w") as f:
            f.write("def real():\n    return 1\n")
        subprocess.run(["git", "-C", sibling, "add", "real.py"], check=True)
        subprocess.run(
            ["git", "-C", sibling, "commit", "-q", "-m", f"[TASK-{task_id}] real"],
            check=True,
        )
        return sibling

    def test_falls_back_to_sibling_worktree(self, tmp_path, monkeypatch):
        primary, _db_path = _make_repo(tmp_path, default_branch="main")
        sibling = self._add_sibling_worktree(primary, task_id=88)

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        # Invoked from the primary checkout (no [TASK-88] commit reachable
        # from HEAD here). After TASK-412 the all-refs commit-grep finds
        # the sibling worktree's commit through the shared object database
        # without needing the secondary worktree-list fallback.
        result = mod.compute_range(88, primary, db_path=None)

        assert result["diff_lines"] > 0
        # Range may be the primary `main...HEAD` form (legacy worktree-
        # fallback path) or the SHA-range `<sha>^..<sha>` form (TASK-412
        # all-refs grep path). Both are valid resolutions of the sibling's
        # diff; the substantive check is that the diff content matches.
        assert (
            result["range"].endswith("HEAD")
            or "..." in result["range"]
            or "^.." in result["range"]
        )
        # The diff content should reference the file from the sibling worktree.
        sibling_diff = subprocess.run(
            ["git", "-C", sibling, "diff", "main...HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
        # Primary's compute_range result should match the sibling's actual diff.
        # (The summary is truncated to SUMMARY_CHARS so just compare prefix.)
        assert result["summary"][:60] == sibling_diff[:60]

    def test_no_sibling_means_clear_error(self, tmp_path, monkeypatch):
        """No sibling worktree carries ``feature/TASK-99-*`` → SystemExit
        with the updated message that names the invoking checkout (so the
        user knows which repo state was actually consulted)."""
        primary, _db_path = _make_repo(tmp_path, default_branch="main")
        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        with pytest.raises(SystemExit) as exc:
            mod.compute_range(99, primary, db_path=None)
        msg = str(exc.value)
        assert "TASK-99" in msg
        assert "No sibling worktree" in msg or "checkout" in msg.lower()

    def test_find_task_feature_worktree_skips_invoking(self, tmp_path):
        """The lookup must not return the invoking worktree as its own
        sibling — otherwise the recursive compute_range call would loop."""
        primary, _db_path = _make_repo(tmp_path, default_branch="main")
        sibling = self._add_sibling_worktree(primary, task_id=70)

        # Invoked from sibling: feature branch is on the invoking worktree,
        # so the function should return None.
        assert mod._find_task_feature_worktree(70, sibling) is None
        # Invoked from primary: sibling is discovered.
        found = mod._find_task_feature_worktree(70, primary)
        assert found is not None
        assert os.path.realpath(found) == os.path.realpath(sibling)


class TestPrimaryRangeTaskCommitCoverage:
    """Issue #821 / TASK-412: ``compute_range`` must verify the chosen primary
    range actually contains a ``[TASK-<id>]`` commit. When the orchestrator's
    CWD is the primary checkout and that checkout has unpushed local-default
    commits unrelated to the task, ``origin/<default>...HEAD`` is non-empty-
    but-wrong; without the validation, ``validate-comments`` runs ``git diff``
    against that range and silently dismisses every legitimate review finding.
    """

    def test_falls_through_when_primary_range_has_no_task_commits(
        self, tmp_path, monkeypatch
    ):
        """Reproduces #821: primary checkout has unpushed local-default commits,
        sibling worktree carries the feature branch. The primary range is non-
        empty (it covers the unpushed commits) but contains no [TASK-N] commit,
        so compute_range must fall through to the all-refs commit-grep and
        return the feature branch's SHA range against the sibling worktree.
        """
        primary, _db_path = _make_repo(tmp_path, default_branch="main")

        # Stand up an origin/main pointing at the seed commit so the primary
        # range is `origin/main...HEAD`. Without this, the helper falls back
        # to local `main...HEAD` and the unpushed-local-main shape doesn't
        # reproduce.
        seed_sha = subprocess.run(
            ["git", "-C", primary, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", primary, "update-ref", "refs/remotes/origin/main", seed_sha],
            check=True,
        )

        # Add an unpushed commit on local main (NOT tagged [TASK-N]).
        with open(os.path.join(primary, "unrelated.txt"), "w") as f:
            f.write("unrelated\n")
        subprocess.run(["git", "-C", primary, "add", "unrelated.txt"], check=True)
        subprocess.run(
            ["git", "-C", primary, "commit", "-q", "-m", "Unrelated local change"],
            check=True,
        )

        # Add a sibling worktree carrying the task's feature branch.
        sibling = primary + "-wt-44"
        subprocess.run(
            ["git", "-C", primary, "worktree", "add", "-b",
             "feature/TASK-44-x", sibling, "refs/remotes/origin/main"],
            check=True, capture_output=True,
        )
        with open(os.path.join(sibling, "task.py"), "w") as f:
            f.write("def task():\n    return 'task'\n")
        subprocess.run(["git", "-C", sibling, "add", "task.py"], check=True)
        subprocess.run(
            ["git", "-C", sibling, "commit", "-q", "-m", "[TASK-44] task work"],
            check=True,
        )
        task_sha = subprocess.run(
            ["git", "-C", sibling, "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout.strip()

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        # Invoked from primary, whose `origin/main...HEAD` would erroneously
        # surface the "Unrelated local change" diff if the validation step
        # weren't in place.
        result = mod.compute_range(44, primary, db_path=None)

        # The returned range must cover the feature branch's task commit,
        # not the unrelated local-main commit.
        assert result["recovered_from_task_commits"] is True
        assert task_sha[:7] in result["range"]
        # task.py is what the feature branch added; unrelated.txt must NOT
        # appear in the resolved diff.
        assert "task.py" in result["summary"]
        assert "unrelated.txt" not in result["summary"]


class TestSiblingHint:
    """Issue #817 / TASK-412: when ``compute_range`` raises "No changes found"
    AND a sibling worktree carries ``feature/TASK-<id>-*``, the error must
    name that worktree path so the operator knows where to re-run from.

    Issue #842: with the block-level scope filter and fallthrough, the
    "filter drops everything" raise path is subsumed — when a scope
    signal exists but no block intersects it, the function returns the
    over-broad range rather than refusing. The sibling-hint enrichment
    is still exercised by the other raise paths in ``compute_range``
    (covered by ``TestWorktreeFallback::test_no_sibling_means_clear_error``)."""

    def test_filter_no_longer_drops_everything_falls_through_842(
        self, tmp_path, monkeypatch
    ):
        """Pre-#842 behavior: when [TASK-N] commits exist in the sibling
        worktree but none overlap the task's referenced paths, the
        prefix-collision filter emptied the candidate list and the
        function raised a #656 SystemExit with the sibling hint.

        Post-#842: the fallthrough returns the off-scope commits as the
        recovered range instead of refusing — over-inclusion is
        recoverable, silent zero-range refusal is not."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=88,
            summary="Wire foo",
            description="Update bin/tusk-foo.py for the foo case",
        )

        # Sibling worktree carrying feature/TASK-88-* with a [TASK-88] commit
        # whose diff DOES NOT overlap with bin/tusk-foo.py.
        sibling = str(tmp_path / "sibling-wt")
        subprocess.run(
            ["git", "-C", repo_root, "worktree", "add", "-b",
             "feature/TASK-88-x", sibling],
            check=True, capture_output=True,
        )
        with open(os.path.join(sibling, "unrelated-area.txt"), "w") as f:
            f.write("touches neither bin/ nor anything else this task names\n")
        subprocess.run(["git", "-C", sibling, "add", "unrelated-area.txt"], check=True)
        subprocess.run(
            ["git", "-C", sibling, "commit", "-q", "-m", "[TASK-88] off-scope"],
            check=True,
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        result = mod.compute_range(88, repo_root, db_path)
        assert result["recovered_from_task_commits"] is True
        assert result["diff_lines"] > 0
        assert "unrelated-area.txt" in result["summary"]


class TestStartedAtScope:
    def test_recovery_excludes_task_commits_before_started_at(self, tmp_path, monkeypatch):
        """Issue #494: recycled task IDs from before the current task lifetime
        must not be used to reconstruct a review diff."""
        repo_root, db_path = _make_repo(tmp_path, default_branch="main")
        _seed_db(
            db_path,
            task_id=7,
            summary="New TASK-7",
            description="No current implementation commits yet.",
            started_at="2026-04-19 10:00:00",
        )

        old_file = os.path.join(repo_root, "old-task-7.txt")
        with open(old_file, "w", encoding="utf-8") as f:
            f.write("old incarnation\n")
        subprocess.run(["git", "-C", repo_root, "add", "old-task-7.txt"], check=True)
        subprocess.run(
            [
                "git", "-C", repo_root,
                "-c", "user.email=t@t",
                "-c", "user.name=t",
                "commit", "-q", "-m", "[TASK-7] old incarnation",
                "--date", "2026-01-15 10:00:00 +0000",
            ],
            check=True,
            env={**os.environ, "GIT_COMMITTER_DATE": "2026-01-15 10:00:00 +0000"},
        )

        monkeypatch.setattr(mod, "default_branch", lambda _repo: "main")

        with pytest.raises(SystemExit) as exc:
            mod.compute_range(7, repo_root, db_path)
        assert "[TASK-7] commits not detected" in str(exc.value)
