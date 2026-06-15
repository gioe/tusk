"""Integration tests for tusk commit reusing a same-HEAD precheck verdict (issue #1083).

When the configured test_command fails at HEAD for reasons unrelated to the
staged change, a prior `tusk test-precheck` records a pre_existing=true verdict
keyed by (HEAD sha, test_command). tusk commit's test gate then reuses that
verdict instead of refusing with exit 2: it lands the commit through the normal
path and stamps a `[test-precheck-bypass]` note into the message body. With no
matching verdict, the gate still refuses with exit 2.

These tests drive tusk-commit.py as a subprocess with a deterministically
failing test_command, seeding precheck_verdicts directly to stand in for a
prior `tusk test-precheck` run.
"""

import json
import os
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")

FAILING_CMD = "false"


def _git_init(repo: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "root"], check=True)


def _head_sha(repo: str) -> str:
    res = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return res.stdout.strip()


def _write_config(tmp_path) -> str:
    with open(CONFIG_DEFAULT, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["test_command"] = FAILING_CMD
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def _make_db_with_verdict(repo: str, head_sha, pre_existing) -> None:
    """Create repo/tusk/tasks.db with the precheck_verdicts table, optionally
    seeding a verdict row. Pass head_sha=None to leave the table empty."""
    os.makedirs(os.path.join(repo, "tusk"), exist_ok=True)
    db = os.path.join(repo, "tusk", "tasks.db")
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE precheck_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                session_id INTEGER,
                head_sha TEXT NOT NULL,
                test_command TEXT NOT NULL,
                pre_existing INTEGER NOT NULL,
                exit_code INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        if head_sha is not None:
            conn.execute(
                "INSERT INTO precheck_verdicts "
                "(head_sha, test_command, pre_existing, exit_code) "
                "VALUES (?, ?, ?, 1)",
                (head_sha, FAILING_CMD, pre_existing),
            )
        conn.commit()
    finally:
        conn.close()


def _run_commit(repo: str, config_path: str, *files: str):
    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    return subprocess.run(
        ["python3", TUSK_COMMIT_PY, repo, config_path, "999", "msg", *files],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
    )


def _commit_count(repo: str) -> int:
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return len(log.stdout.strip().splitlines())


def _last_message(repo: str) -> str:
    res = subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%B"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return res.stdout


def test_reuse_lands_commit_with_bypass_note(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    config_path = _write_config(tmp_path)
    _make_db_with_verdict(repo, _head_sha(repo), pre_existing=1)

    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, "code.py")

    assert result.returncode == 0, (
        f"expected reuse to land the commit, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _commit_count(repo) == 2, "the commit should land despite the failing gate"
    assert "[test-precheck-bypass]" in _last_message(repo)


def test_no_verdict_still_refuses_with_exit_2(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    config_path = _write_config(tmp_path)
    _make_db_with_verdict(repo, head_sha=None, pre_existing=0)  # empty table

    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, "code.py")

    assert result.returncode == 2, (
        f"expected exit 2 with no verdict, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _commit_count(repo) == 1, "no commit should land"
    assert "[test-precheck-bypass]" not in _last_message(repo)


def test_pre_existing_false_verdict_refuses_with_exit_2(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    config_path = _write_config(tmp_path)
    _make_db_with_verdict(repo, _head_sha(repo), pre_existing=0)

    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, "code.py")

    assert result.returncode == 2, (
        f"a pre_existing=false verdict must not be reused, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _commit_count(repo) == 1


def test_precheck_records_verdict_for_commit_reuse(tmp_path):
    """End-to-end: tusk test-precheck writes a verdict row that tusk commit then
    reuses — no hand-seeded row."""
    repo = str(tmp_path / "repo")
    _git_init(repo)
    config_path = _write_config(tmp_path)
    # Create the DB with the table but no rows; test-precheck populates it.
    _make_db_with_verdict(repo, head_sha=None, pre_existing=0)

    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    precheck = subprocess.run(
        ["python3", os.path.join(REPO_ROOT, "bin", "tusk-test-precheck.py"),
         repo, config_path, "--command", FAILING_CMD],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
    )
    assert precheck.returncode == 0, (
        f"precheck failed: {precheck.stdout}\n{precheck.stderr}"
    )
    verdict = json.loads(precheck.stdout)
    assert verdict["pre_existing"] is True

    db = os.path.join(repo, "tusk", "tasks.db")
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT head_sha, test_command, pre_existing FROM precheck_verdicts"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "test-precheck should record a verdict row"
    assert row[0] == _head_sha(repo)
    assert row[1] == FAILING_CMD
    assert row[2] == 1

    # Now commit a code file — the recorded verdict must be reused.
    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    result = _run_commit(repo, config_path, "code.py")
    assert result.returncode == 0, (
        f"expected reuse of the recorded verdict, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "[test-precheck-bypass]" in _last_message(repo)
