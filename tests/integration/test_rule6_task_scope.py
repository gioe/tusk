"""Integration tests for Rule 6 task-scoping (Issue #568 / TASK-188).

Reproduces the original failure mode: a `tusk commit` against TASK-A used to
fail Rule 6 because an unrelated TASK-B was sitting in Done with incomplete
acceptance criteria. After the fix, `tusk commit` passes `--task <task_id>`
to the lint subprocess and Rule 6 narrows its query to that single task.

Three scoping modes are pinned here:

- Standalone `tusk lint` (no `--task`) keeps the original global behavior —
  it scans every Done task closed in the last 30 days.
- `tusk lint --task <unrelated_id>` ignores TASK-B entirely and reports no
  Rule 6 violations.
- `tusk lint --task <Done_task_id>` does fire when the scoped task itself
  is Done with incomplete criteria (regression guard against over-narrowing).

Plus an end-to-end check: `tusk commit` against TASK-A succeeds even with
TASK-B sitting in the DB as a Done-with-incomplete-criteria timebomb.
"""

import json
import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
TUSK_LINT_PY = os.path.join(REPO_ROOT, "bin", "tusk-lint.py")
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")


def _env_for(repo, tmp_path, **overrides):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TUSK_STATE_DIR": str(tmp_path / "state"),
        "TUSK_PROJECT": str(repo),
        "TUSK_QUIET": "1",
    }
    env.pop("TUSK_DB", None)
    env.update(overrides)
    return env


def _git_init(repo):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "root"], cwd=str(repo), check=True
    )


def _tusk(repo, env, *args, check=True):
    r = subprocess.run(
        [TUSK_BIN, *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check:
        assert r.returncode == 0, (
            f"tusk {' '.join(args)} failed (exit {r.returncode})\n"
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
    return r


@pytest.fixture
def setup_repo(tmp_path):
    """Init a fake repo, insert TASK-A (active) and TASK-B (Done with
    incomplete criterion), and return (repo, env, task_a_id, task_b_id)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    env = _env_for(repo, tmp_path)
    _tusk(repo, env, "init")

    # TASK-A — the "active" task, will host the regression-test commit.
    a = json.loads(
        _tusk(
            repo, env,
            "task-insert",
            "Active task A", "the task we are committing for",
            "--priority", "Medium", "--task-type", "feature", "--complexity", "S",
            "--criteria", "criterion A1",
        ).stdout
    )
    task_a_id = a["task_id"]
    a_crit_id = a["criteria_ids"][0]

    # TASK-B — unrelated Done task with an incomplete criterion. Inserting
    # it as To Do, then closing via direct SQL (tusk task-done would refuse
    # to close while criteria are incomplete) — this is the exact state that
    # used to make Rule 6 block unrelated commits.
    b = json.loads(
        _tusk(
            repo, env,
            "task-insert",
            "Unrelated finished task B",
            "B was closed prematurely with an open criterion",
            "--priority", "Medium", "--task-type", "feature", "--complexity", "S",
            "--criteria", "criterion B1 (left incomplete)",
        ).stdout
    )
    task_b_id = b["task_id"]

    _tusk(
        repo, env,
        "UPDATE tasks SET status = 'Done', closed_reason = 'completed', "
        "closed_at = datetime('now'), updated_at = datetime('now') "
        f"WHERE id = {task_b_id}",
    )

    # Mark TASK-A's criterion done so a commit on A wouldn't trip the
    # scoped Rule 6 against A itself in the e2e test below.
    _tusk(repo, env, "criteria", "done", str(a_crit_id), "--skip-verify")

    return repo, env, task_a_id, task_b_id


class TestRule6Scoping:
    def test_global_lint_still_flags_unrelated_done_task(self, setup_repo):
        """Standalone `tusk lint` (no --task) preserves global behavior."""
        repo, env, _task_a_id, task_b_id = setup_repo

        r = subprocess.run(
            ["python3", TUSK_LINT_PY, str(repo), "--quiet"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

        assert r.returncode == 1, (
            f"expected Rule 6 to fire (exit 1), got {r.returncode}\n"
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
        assert "Rule 6" in r.stdout
        assert f"TASK-{task_b_id}" in r.stdout

    def test_scoped_lint_ignores_unrelated_task(self, setup_repo):
        """`--task <unrelated_id>` skips Rule 6 against TASK-B."""
        repo, env, task_a_id, task_b_id = setup_repo

        r = subprocess.run(
            ["python3", TUSK_LINT_PY, str(repo),
             "--quiet", "--task", str(task_a_id)],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

        # No Rule 6 violation — scoping to TASK-A skips B's bad state.
        # (Other rules may or may not fire depending on the bare repo state;
        # we only assert that Rule 6 does NOT mention TASK-B.)
        assert "Rule 6" not in r.stdout, (
            f"Rule 6 should not fire when scoped to unrelated task\n"
            f"stdout={r.stdout}"
        )
        assert f"TASK-{task_b_id}" not in r.stdout

    def test_scoped_lint_still_blocks_when_self_is_bad(self, setup_repo):
        """`--task <id>` against a Done-with-incomplete task still fires Rule 6."""
        repo, env, _task_a_id, task_b_id = setup_repo

        r = subprocess.run(
            ["python3", TUSK_LINT_PY, str(repo),
             "--quiet", "--task", str(task_b_id)],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

        assert r.returncode == 1, (
            f"expected Rule 6 to fire when scoped to its own bad task\n"
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
        assert "Rule 6" in r.stdout
        assert f"TASK-{task_b_id}" in r.stdout


class TestCommitNotBlockedByUnrelatedDoneTask:
    def test_commit_succeeds_with_unrelated_bad_done_task_in_db(self, setup_repo):
        """End-to-end: a commit on TASK-A succeeds even though TASK-B sits
        in Done with incomplete criteria (the original Issue #568 repro)."""
        repo, env, task_a_id, _task_b_id = setup_repo

        target = repo / "payload.txt"
        target.write_text("hello\n")

        r = subprocess.run(
            ["python3", TUSK_COMMIT_PY, str(repo), CONFIG_DEFAULT,
             str(task_a_id), "msg", str(target)],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=str(repo),
        )

        assert r.returncode == 0, (
            f"commit on TASK-{task_a_id} must not be blocked by TASK-B's bad "
            f"state — got exit {r.returncode}\n"
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
