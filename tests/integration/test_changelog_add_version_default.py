"""Integration tests for tusk changelog-add optional version + drift detection (issue #814).

Before this change, `tusk changelog-add <version> <task_id>` accepted the
positional `<version>` verbatim with no cross-check against the VERSION file.
A chained `tusk version-bump && tusk changelog-add 919 <task_id>` against a
VERSION already at 920 silently wrote `## [919] - YYYY-MM-DD` instead of
`## [920]`. Now:

- Omit `<version>` and changelog-add reads the VERSION file (the canonical source).
- Pass `--from-version-file` to explicitly opt into that behavior with
  positional args treated entirely as task IDs.
- Pass `<version>` explicitly and changelog-add cross-checks it against the
  VERSION file; a mismatch is rejected with a helpful error.
- Pass `--help` and argparse intercepts before any CHANGELOG write happens
  (the prior implementation would write `## [--help]` to CHANGELOG).
"""

import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _git(args, *, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return result


def _tusk(args, *, cwd, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _seed_repo(tmp_path, version="999"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "VERSION", "CHANGELOG.md", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _env_with_db(repo):
    env = os.environ.copy()
    env["TUSK_DB"] = str(repo / "tusk" / "tasks.db")
    env["TUSK_QUIET"] = "1"
    return env


def test_no_args_reads_version_file(tmp_path):
    """`tusk changelog-add` with no args sources version from VERSION file."""
    repo = _seed_repo(tmp_path, version="500")
    env = _env_with_db(repo)

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [500] - " in changelog
    assert "## [--help]" not in changelog


def test_drift_errors_clearly(tmp_path):
    """Passing a version that disagrees with VERSION file aborts before writing."""
    repo = _seed_repo(tmp_path, version="500")
    env = _env_with_db(repo)
    original_changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    result = _tusk(["changelog-add", "499"], cwd=repo, env=env)

    assert result.returncode != 0, "Expected non-zero exit on version mismatch"
    assert "disagrees with VERSION file content" in result.stderr
    assert "'499'" in result.stderr
    assert "'500'" in result.stderr
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == original_changelog


def test_explicit_matching_version_succeeds(tmp_path):
    """Passing the same version as VERSION file is accepted (backwards compat)."""
    repo = _seed_repo(tmp_path, version="500")
    env = _env_with_db(repo)

    result = _tusk(["changelog-add", "500"], cwd=repo, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [500] - " in changelog


def test_from_version_file_flag(tmp_path):
    """--from-version-file sources the version even when positionals are present."""
    repo = _seed_repo(tmp_path, version="500")
    env = _env_with_db(repo)

    result = _tusk(["changelog-add", "--from-version-file"], cwd=repo, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [500] - " in changelog


def test_help_does_not_write_changelog(tmp_path):
    """`tusk changelog-add --help` prints usage; it must not corrupt CHANGELOG."""
    repo = _seed_repo(tmp_path, version="500")
    env = _env_with_db(repo)
    original_changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    result = _tusk(["changelog-add", "--help"], cwd=repo, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower()
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == original_changelog


def _init_db_with_task(repo, env, task_id: int):
    """Initialise the tusk DB inside ``repo`` and insert a single task whose
    ``id`` equals ``task_id``, so ``tasks.id`` can be used as a disambiguator
    in the heuristic under test."""
    init = subprocess.run(
        [TUSK_BIN, "init", "--force"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert init.returncode == 0, init.stderr
    insert = subprocess.run(
        [
            TUSK_BIN, "task-insert",
            "task-id-detect-target",
            "test task for #902 disambiguation",
            "--priority", "Low",
            "--task-type", "feature",
            "--complexity", "XS",
            "--criteria", "noop",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert insert.returncode == 0, insert.stderr
    new_id = int(__import__("json").loads(insert.stdout)["task_id"])
    if new_id != task_id:
        sqlite3 = __import__("sqlite3")
        conn = sqlite3.connect(env["TUSK_DB"])
        conn.execute("UPDATE tasks SET id = ? WHERE id = ?", (task_id, new_id))
        conn.commit()
        conn.close()


def test_bare_task_id_routes_to_version_file(tmp_path):
    """Passing a single positional that matches a tasks.id row treats it as
    a task ID, not a version (issue #902). The result must be identical to
    `tusk changelog-add --from-version-file <task_id>`."""
    repo = _seed_repo(tmp_path, version="998")
    env = _env_with_db(repo)
    _init_db_with_task(repo, env, task_id=473)

    result = _tusk(["changelog-add", "473"], cwd=repo, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [998] - " in changelog
    assert "- [TASK-473]" in changelog
    assert "## [473]" not in changelog


def test_bare_non_task_id_still_errors_as_version_mismatch(tmp_path):
    """A bare positional that does NOT match any task row must still hit the
    original drift error — the heuristic must not silently absorb arbitrary
    integers."""
    repo = _seed_repo(tmp_path, version="998")
    env = _env_with_db(repo)
    _init_db_with_task(repo, env, task_id=473)
    original_changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    result = _tusk(["changelog-add", "999999"], cwd=repo, env=env)

    assert result.returncode != 0
    assert "disagrees with VERSION file content" in result.stderr
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == original_changelog


def test_explicit_version_plus_task_id_still_works(tmp_path):
    """Two positionals where the first equals VERSION continues to work as
    `<version> <task_id>` — the heuristic must not break the explicit form."""
    repo = _seed_repo(tmp_path, version="998")
    env = _env_with_db(repo)
    _init_db_with_task(repo, env, task_id=473)

    result = _tusk(["changelog-add", "998", "473"], cwd=repo, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [998] - " in changelog
    assert "- [TASK-473]" in changelog


def test_missing_version_file_with_no_args_errors(tmp_path):
    """No positional version and no VERSION file → clear error, no CHANGELOG mutation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "tusk@example.test"], cwd=repo)
    _git(["config", "user.name", "Tusk Tests"], cwd=repo)
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "CHANGELOG.md", "README.md"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    env = _env_with_db(repo)
    original_changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    result = _tusk(["changelog-add"], cwd=repo, env=env)

    assert result.returncode != 0
    assert "VERSION file not found" in result.stderr
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == original_changelog
