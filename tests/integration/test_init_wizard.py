"""Integration tests for `tusk init-wizard` non-interactive path (TASK-146).

The wizard ports the /tusk-init Claude Code skill to a CLI so Codex users
can configure tusk/config.json without hand-editing. These tests exercise
the flags-only, no-stdin-prompts code path end-to-end against a real tmp
project with its own tusk/ layout and config.json.
"""

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(tmp_path, *args):
    db_file = tmp_path / "tusk" / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file)}
    result = subprocess.run(
        [TUSK_BIN, "init-wizard", *args],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result


def _read_config(tmp_path):
    return json.loads((tmp_path / "tusk" / "config.json").read_text())


@pytest.fixture()
def codex_like_project(tmp_path):
    """A git repo with AGENTS.md and an initialised tusk/ DB + config, mirroring
    a fresh Codex install after install.sh has run."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    (tmp_path / "tusk").mkdir()
    db_file = tmp_path / "tusk" / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file)}
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"tusk init failed:\n{result.stderr}"
    return tmp_path


def test_flags_only_writes_complete_config(codex_like_project):
    """Non-interactive path with explicit flags produces success=true and
    persists every requested key verbatim into tusk/config.json."""
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--domains", '["api","frontend"]',
        "--agents", '{"backend":"API + DB","frontend":"UI"}',
        "--task-types", '["bug","feature","test"]',
        "--test-command", "pytest -q",
    )
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["mode"] == "non-interactive"

    cfg = _read_config(codex_like_project)
    assert cfg["domains"] == ["api", "frontend"]
    assert cfg["agents"] == {"backend": "API + DB", "frontend": "UI"}
    assert cfg["task_types"] == ["bug", "feature", "test"]
    assert cfg["test_command"] == "pytest -q"


def test_auto_scan_derives_domains_from_project_signals(codex_like_project):
    """With --auto-scan, the wizard calls init-scan-codebase + test-detect and
    populates domains, agents, task_types, and test_command without any
    overrides. Agents are derived from the domain mapping and must be plain
    string values (config validator rejects dicts)."""
    (codex_like_project / "src" / "components").mkdir(parents=True)
    (codex_like_project / "routes").mkdir()
    (codex_like_project / "docs").mkdir()
    (codex_like_project / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndependencies = ["fastapi>=0.100"]\n'
    )

    result = _run(codex_like_project, "--non-interactive", "--auto-scan")
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True

    cfg = _read_config(codex_like_project)
    assert "frontend" in cfg["domains"], cfg["domains"]
    assert "api" in cfg["domains"], cfg["domains"]
    assert "general" in cfg["agents"], cfg["agents"]
    assert all(isinstance(v, str) for v in cfg["agents"].values()), (
        "agents values must be strings — config validator rejects dict values"
    )
    assert cfg["task_types"], "task_types should be populated with defaults"


def test_explicit_flags_win_over_scan_defaults(codex_like_project):
    """When both --auto-scan and explicit flags are given, the flags must
    override the scan-derived values."""
    (codex_like_project / "src" / "components").mkdir(parents=True)
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--auto-scan",
        "--domains", '["custom"]',
    )
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    cfg = _read_config(codex_like_project)
    assert cfg["domains"] == ["custom"]


def test_project_type_auto_populates_project_libs(codex_like_project):
    """Passing --project-type for a known built-in causes init-write-config to
    auto-merge the matching project_libs entry from config.default.json."""
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--project-type", "python_service",
    )
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    cfg = _read_config(codex_like_project)
    assert cfg["project_type"] == "python_service"
    assert "python_service" in cfg.get("project_libs", {}), cfg.get("project_libs")


def test_invalid_json_flag_reports_error_without_mutation(codex_like_project):
    """Bad JSON in a config flag produces success=false with a clear error and
    does not touch tusk/config.json."""
    before = _read_config(codex_like_project)
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--domains", "not-a-json-array",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert "domains" in payload["error"].lower()
    after = _read_config(codex_like_project)
    assert after == before, "invalid-flag failure must not mutate config.json"


def test_seed_bootstrap_tasks_none_skips_fetch(codex_like_project):
    """Default --seed-bootstrap-tasks=none in non-interactive mode leaves
    seeded_tasks empty even when project_libs has entries."""
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--project-libs", '{"my_lib": {"repo": "does/not-exist", "ref": "main"}}',
    )
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["seeded_tasks"] == []
    assert payload["skipped_tasks"] == []


def test_no_auto_scan_leaves_domains_empty_when_no_overrides(codex_like_project):
    """Without --auto-scan and without explicit flags, the wizard writes nothing
    to existing keys — they carry forward from init-write-config's merge."""
    before = _read_config(codex_like_project)
    result = _run(codex_like_project, "--non-interactive", "--no-auto-scan")
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    after = _read_config(codex_like_project)
    # The merge step in init-write-config preserves unspecified keys, so the
    # config should be unchanged when nothing is passed.
    assert after == before


def test_scaffold_spec_invokes_init_scaffold(codex_like_project):
    """`--scaffold-spec '<json>'` runs `tusk init-scaffold` after writing config,
    creating each directory with .gitkeep + AGENTS.md (codex mode), and embeds
    the scaffold result under the `scaffold` key in the wizard payload."""
    spec = json.dumps([
        {"name": "frontend", "purpose": "UI sources", "agent": "frontend"},
        {"name": "backend",  "purpose": "API code",   "agent": "backend"},
    ])
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--scaffold-spec", spec,
    )
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    scaffold = payload["scaffold"]
    assert scaffold is not None and scaffold["success"] is True
    assert scaffold["mode"] == "codex"
    assert {c["directory"] for c in scaffold["created"]} == {"frontend", "backend"}

    for sub in ("frontend", "backend"):
        assert (codex_like_project / sub / ".gitkeep").exists()
        assert (codex_like_project / sub / "AGENTS.md").exists()


def test_no_scaffold_explicit_opt_out(codex_like_project):
    """`--no-scaffold` succeeds with `scaffold: null` and creates no directories."""
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--no-scaffold",
    )
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["scaffold"] is None
    assert not (codex_like_project / "frontend").exists()


def test_non_interactive_default_no_scaffold(codex_like_project):
    """`--non-interactive` without either scaffold flag defaults to no-scaffold:
    `scaffold: null` and no directories created."""
    result = _run(codex_like_project, "--non-interactive", "--no-auto-scan")
    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["scaffold"] is None


def test_scaffold_flags_mutually_exclusive(codex_like_project):
    """Passing both `--scaffold-spec` and `--no-scaffold` exits non-zero with a
    clear JSON error and does not mutate config or create scaffolded dirs."""
    before = _read_config(codex_like_project)
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--scaffold-spec", "[]",
        "--no-scaffold",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert "mutually exclusive" in payload["error"].lower()
    assert _read_config(codex_like_project) == before


def test_scaffold_spec_invalid_json_fails_clean(codex_like_project):
    """A non-JSON `--scaffold-spec` value fails fast (before init-write-config
    runs) with a clear error and leaves config untouched."""
    before = _read_config(codex_like_project)
    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--scaffold-spec", "not-json",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert "scaffold-spec" in payload["error"].lower()
    assert _read_config(codex_like_project) == before


def test_help_documents_scaffold_flags(codex_like_project):
    """`tusk init-wizard --help` exits 0, prints documentation for both new
    flags, and does NOT mutate config.json (regression: the wizard's
    `parse_known_args` previously dropped --help silently, then ran the wizard
    with side effects)."""
    before = _read_config(codex_like_project)
    result = _run(codex_like_project, "--help")
    assert result.returncode == 0, f"--help failed:\n{result.stderr}"
    assert "--scaffold-spec" in result.stdout
    assert "--no-scaffold" in result.stdout
    assert _read_config(codex_like_project) == before
