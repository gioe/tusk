"""Integration tests for the non-code-only test-gate skip (issue #950).

`tusk commit` invokes the configured test_command even when a commit stages
only VERSION and CHANGELOG.md — files that cannot change test outcomes. This
wastes wall-clock on every version-bump commit and exposes them to timeout
flakes under load. The fix info-skips the gate when every staged file is a
docs/markdown file, a GitHub workflow YAML file, or a scope.always_allowed
metadata file; lint and pre-commit hooks still run, and the gate still runs
whenever any code file is staged.

Each test drives tusk-commit.py as a subprocess with a test_command that
writes a sentinel marker file, then asserts whether the marker was created —
the authoritative signal for whether the gate actually ran.
"""

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")


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


def _write_config(
    tmp_path,
    marker_path,
    *,
    always_allowed=None,
    drop_scope=False,
    path_test_commands=None,
    path_test_commands_skip_unmatched=None,
) -> str:
    """Write a config.json based on config.default.json with a sentinel-writing
    test_command. The gate, when it runs, creates ``marker_path``."""
    with open(CONFIG_DEFAULT, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["test_command"] = f"touch {marker_path}"
    if path_test_commands is not None:
        cfg["path_test_commands"] = path_test_commands
    if path_test_commands_skip_unmatched is not None:
        cfg["path_test_commands_skip_unmatched"] = path_test_commands_skip_unmatched
    if drop_scope:
        cfg.pop("scope", None)
    elif always_allowed is not None:
        cfg.setdefault("scope", {})["always_allowed"] = always_allowed
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def _run_commit(repo: str, config_path: str, *files: str):
    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    return subprocess.run(
        ["python3", TUSK_COMMIT_PY, repo, config_path, "999", "msg", *files],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
        env=env,
    )


def _commit_count(repo: str) -> int:
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return len(log.stdout.strip().splitlines())


def test_version_and_changelog_only_skips_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("2\n")
    with open(os.path.join(repo, "CHANGELOG.md"), "w", encoding="utf-8") as f:
        f.write("## [2] - 2026-05-28\n")

    result = _run_commit(repo, config_path, "VERSION", "CHANGELOG.md")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert not marker.exists(), "test gate ran on a non-code-only commit"
    assert "skipping test gate" in result.stdout
    assert _commit_count(repo) == 2, "the commit should still land"


def test_markdown_only_skips_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    os.makedirs(os.path.join(repo, "docs"), exist_ok=True)
    with open(os.path.join(repo, "docs", "NOTES.md"), "w", encoding="utf-8") as f:
        f.write("# notes\n")

    result = _run_commit(repo, config_path, "docs/NOTES.md")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert not marker.exists(), "test gate ran on a markdown-only commit"
    assert _commit_count(repo) == 2


def test_github_workflow_yaml_only_skips_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    workflows = os.path.join(repo, ".github", "workflows")
    os.makedirs(workflows, exist_ok=True)
    with open(os.path.join(workflows, "web-ci.yml"), "w", encoding="utf-8") as f:
        f.write("name: web\n")
    with open(os.path.join(workflows, "scraper-ci.yaml"), "w", encoding="utf-8") as f:
        f.write("name: scraper\n")

    result = _run_commit(
        repo,
        config_path,
        ".github/workflows/web-ci.yml",
        ".github/workflows/scraper-ci.yaml",
    )

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert not marker.exists(), "test gate ran on a workflow-yaml-only commit"
    assert "skipping test gate" in result.stdout
    assert _commit_count(repo) == 2


def test_non_workflow_yaml_runs_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    dependabot_dir = os.path.join(repo, ".github")
    os.makedirs(dependabot_dir, exist_ok=True)
    with open(os.path.join(dependabot_dir, "dependabot.yml"), "w", encoding="utf-8") as f:
        f.write("version: 2\n")

    result = _run_commit(repo, config_path, ".github/dependabot.yml")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.exists(), "test gate must run for non-workflow YAML"
    assert "skipping test gate" not in result.stdout


def test_path_scope_unmatched_yaml_skips_gate_when_configured(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    global_marker = tmp_path / "global_gate_ran"
    path_marker = tmp_path / "path_gate_ran"
    config_path = _write_config(
        tmp_path,
        global_marker,
        path_test_commands={"apps/scraper/*": f"touch {path_marker}"},
        path_test_commands_skip_unmatched=True,
    )

    github_dir = os.path.join(repo, ".github")
    os.makedirs(github_dir, exist_ok=True)
    with open(os.path.join(github_dir, "dependabot.yml"), "w", encoding="utf-8") as f:
        f.write("version: 2\n")

    result = _run_commit(repo, config_path, ".github/dependabot.yml")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert not global_marker.exists(), "global test gate ran for an unmatched path"
    assert not path_marker.exists(), "path test gate ran for an unmatched path"
    assert _commit_count(repo) == 2


def test_path_scope_covered_path_runs_path_command_when_skip_configured(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    global_marker = tmp_path / "global_gate_ran"
    path_marker = tmp_path / "path_gate_ran"
    config_path = _write_config(
        tmp_path,
        global_marker,
        path_test_commands={"apps/scraper/*": f"touch {path_marker}"},
        path_test_commands_skip_unmatched=True,
    )

    scraper_dir = os.path.join(repo, "apps", "scraper")
    os.makedirs(scraper_dir, exist_ok=True)
    with open(os.path.join(scraper_dir, "job.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, "apps/scraper/job.py")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert path_marker.exists(), "path-specific test gate did not run"
    assert not global_marker.exists(), "global test gate ran instead of path command"


def test_path_scope_mixed_covered_and_uncovered_paths_run_global_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    global_marker = tmp_path / "global_gate_ran"
    path_marker = tmp_path / "path_gate_ran"
    config_path = _write_config(
        tmp_path,
        global_marker,
        path_test_commands={"apps/scraper/*": f"touch {path_marker}"},
        path_test_commands_skip_unmatched=True,
    )

    scraper_dir = os.path.join(repo, "apps", "scraper")
    docs_dir = os.path.join(repo, "docs")
    os.makedirs(scraper_dir, exist_ok=True)
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(scraper_dir, "job.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    with open(os.path.join(docs_dir, "NOTES.txt"), "w", encoding="utf-8") as f:
        f.write("notes\n")

    result = _run_commit(repo, config_path, "apps/scraper/job.py", "docs/NOTES.txt")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert global_marker.exists(), "global test gate must run for mixed coverage"
    assert not path_marker.exists(), "path-specific command must not run for mixed paths"


def test_code_file_runs_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, "code.py")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.exists(), "test gate did NOT run on a code commit"
    assert "skipping test gate" not in result.stdout


def test_mixed_workflow_yaml_and_code_runs_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    workflows = os.path.join(repo, ".github", "workflows")
    os.makedirs(workflows, exist_ok=True)
    with open(os.path.join(workflows, "web-ci.yml"), "w", encoding="utf-8") as f:
        f.write("name: web\n")
    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, ".github/workflows/web-ci.yml", "code.py")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.exists(), "gate must run when workflow YAML is staged with code"
    assert "skipping test gate" not in result.stdout


def test_mixed_code_and_noncode_runs_gate(tmp_path):
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("2\n")
    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    result = _run_commit(repo, config_path, "VERSION", "code.py")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.exists(), "test gate must run when any staged file is code"
    assert "skipping test gate" not in result.stdout


def test_pre_staged_code_runs_gate(tmp_path):
    """A code file staged before tusk commit rides along into the path-less
    git commit, so the gate must run even when only VERSION is passed."""
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("2\n")
    with open(os.path.join(repo, "code.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    # Stage the code file out-of-band, then commit only VERSION via tusk.
    subprocess.run(["git", "-C", repo, "add", "code.py"], check=True)

    result = _run_commit(repo, config_path, "VERSION")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.exists(), "gate must run when pre-staged code rides along"
    assert "skipping test gate" not in result.stdout


def test_unstaged_deletion_of_code_runs_gate(tmp_path):
    """An unstaged deletion of a tracked code file is auto-swept into the
    commit by Step 2.5, so the gate must run even when only VERSION is passed."""
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker)

    # Track a code file in a prior commit, then delete it on disk (unstaged).
    with open(os.path.join(repo, "mod.py"), "w", encoding="utf-8") as f:
        f.write("y = 2\n")
    subprocess.run(["git", "-C", repo, "add", "mod.py"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "add mod"], check=True)
    os.remove(os.path.join(repo, "mod.py"))

    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("2\n")

    result = _run_commit(repo, config_path, "VERSION")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker.exists(), "gate must run when a swept-in code deletion is committed"
    assert "skipping test gate" not in result.stdout


def test_version_only_skips_gate_via_default_fallback(tmp_path):
    """A project config that predates scope.always_allowed (key absent) still
    recognizes VERSION as non-code via the canonical default fallback."""
    repo = str(tmp_path / "repo")
    _git_init(repo)
    marker = tmp_path / "gate_ran"
    config_path = _write_config(tmp_path, marker, drop_scope=True)

    with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8") as f:
        f.write("2\n")

    result = _run_commit(repo, config_path, "VERSION")

    assert result.returncode == 0, (
        f"expected success, got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert not marker.exists(), "fallback allowlist should recognize VERSION as non-code"
    assert "skipping test gate" in result.stdout
