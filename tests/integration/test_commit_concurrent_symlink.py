"""Concurrent tusk commit coverage for linked-worktree symlink changes.

Issue #1217 observed one invocation report a Git failure while the shared task
state showed a landed commit and completed criterion. Those outcomes belong to
competing processes: the winner committed, while the loser reached Git after
the working tree was already clean. The operation lock must make that split
explicit before the losing process runs ``git commit``.
"""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
TUSK_BIN = REPO_ROOT / "bin" / "tusk"


def _run(cmd, cwd, env=None, check=True, timeout=None):
    return subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main", repo], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    target = repo / "skills" / "cost"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("cost skill\n", encoding="utf-8")
    _run(["git", "add", "skills/cost/SKILL.md"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "seed"], cwd=repo)


def _summary(stdout: str) -> dict:
    marker = "TUSK_COMMIT_RESULT: "
    line = next(line for line in stdout.splitlines() if line.startswith(marker))
    return json.loads(line[len(marker):])


def test_concurrent_symlink_commit_has_process_specific_collision(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)

    _run([TUSK_BIN, "init", "--yes"], cwd=repo, env=env)
    inserted = _run(
        [
            TUSK_BIN,
            "task-insert",
            "Concurrent symlink commit",
            "Exercise one symlink commit from a linked worktree.",
            "--priority",
            "High",
            "--domain",
            "cli",
            "--task-type",
            "bug",
            "--complexity",
            "S",
            "--criteria",
            "The symlink commit completes once.",
        ],
        cwd=repo,
        env=env,
    )
    payload = json.loads(inserted.stdout)
    task_id = payload["task_id"]
    criterion_id = payload["criteria_ids"][0]

    worktree = tmp_path / "worktree"
    branch = f"feature/TASK-{task_id}-concurrent-symlink"
    _run(["git", "worktree", "add", "-q", "-b", branch, worktree], cwd=repo)
    link_dir = worktree / ".claude" / "skills"
    link_dir.mkdir(parents=True)
    os.symlink("../../skills/cost", link_dir / "cost", target_is_directory=True)

    real_git = shutil.which("git")
    assert real_git is not None
    ready = tmp_path / "git-add-ready"
    release = tmp_path / "git-add-release"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = add ] && [ \"${TUSK_COMMIT_TEST_PAUSE:-}\" = 1 ] "
        "&& [ ! -e \"$TUSK_COMMIT_TEST_READY\" ]; then\n"
        "  : > \"$TUSK_COMMIT_TEST_READY\"\n"
        "  while [ ! -e \"$TUSK_COMMIT_TEST_RELEASE\" ]; do sleep 0.01; done\n"
        "fi\n"
        f"exec {real_git!r} \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    commit_env = {
        **env,
        "PATH": f"{shim_dir}{os.pathsep}{env['PATH']}",
        "TUSK_COMMIT_TEST_PAUSE": "1",
        "TUSK_COMMIT_TEST_READY": str(ready),
        "TUSK_COMMIT_TEST_RELEASE": str(release),
    }
    command = [
        str(TUSK_BIN),
        "commit",
        str(task_id),
        "Track cost skill discovery symlink",
        ".claude/skills/cost",
        "--criteria",
        str(criterion_id),
        "--skip-verify",
        "--allow-branch-mismatch",
    ]

    winner = subprocess.Popen(
        command,
        cwd=worktree,
        env=commit_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and winner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        release.write_text("release\n", encoding="utf-8")
        winner_stdout, winner_stderr = winner.communicate(timeout=5)
        raise AssertionError(
            "winner never reached the paused git add: "
            f"stdout={winner_stdout!r} stderr={winner_stderr!r}"
        )

    try:
        loser = _run(
            command, cwd=worktree, env=commit_env, check=False, timeout=10
        )
    finally:
        release.write_text("release\n", encoding="utf-8")
    winner_stdout, winner_stderr = winner.communicate(timeout=30)

    assert winner.returncode == 0, winner_stderr
    winner_result = _summary(winner_stdout)
    assert winner_result["status"] == "success"
    assert winner_result["commit"]

    assert loser.returncode == 9
    loser_output = loser.stdout + loser.stderr
    assert "another tusk commit invocation is active" in loser_output
    assert "this process did not run git commit" in loser_output
    assert "Error: git commit failed" not in loser_output
    assert _summary(loser.stdout)["exit_code"] == 9

    task_commits = _run(
        ["git", "log", "--format=%H", "--grep", f"^\\[TASK-{task_id}\\]"],
        cwd=worktree,
    ).stdout.splitlines()
    assert len(task_commits) == 1
    assert task_commits[0].startswith(winner_result["commit"])

    with sqlite3.connect(repo / "tusk" / "tasks.db") as conn:
        completed, commit_hash = conn.execute(
            "SELECT is_completed, commit_hash FROM acceptance_criteria WHERE id = ?",
            (criterion_id,),
        ).fetchone()
    assert completed == 1
    assert task_commits[0].startswith(commit_hash)
