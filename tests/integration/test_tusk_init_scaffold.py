"""Integration tests for `tusk init-scaffold` and the relaxed install.sh git check (TASK-212).

The fresh-project path of /tusk-init proposes a directory skeleton with per-directory
CLAUDE.md / AGENTS.md routing stubs. These tests pin three behaviours end-to-end:

1. `tusk init-scaffold` creates each directory with a `.gitkeep` and an
   install-mode-aware stub (CLAUDE.md in Claude installs, AGENTS.md in Codex).
2. Existing directories with files are skipped — user code is never overwritten.
3. `install.sh` no longer hard-fails outside a git repo; fresh, not-yet-initialised
   projects can run it and get a working install (the `.git/hooks/` dispatcher
   block already handles missing git gracefully).
"""

import json
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")


def _scaffold(tmp_path, spec, *, mode=None, repo_root=None):
    """Run `tusk init-scaffold` with the given JSON spec; return parsed stdout JSON."""
    db_file = tmp_path / "tusk" / "tasks.db"
    env = {**os.environ, "TUSK_DB": str(db_file)}
    args = [TUSK_BIN, "init-scaffold", "--spec", json.dumps(spec)]
    if mode is not None:
        args += ["--mode", mode]
    if repo_root is not None:
        args += ["--repo-root", str(repo_root)]
    result = subprocess.run(
        args,
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"init-scaffold failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    return json.loads(result.stdout)


@pytest.fixture()
def claude_project(tmp_path):
    """A tmp project with a .claude/ marker — install-mode auto-detect → claude."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    (tmp_path / ".claude").mkdir()
    return tmp_path


@pytest.fixture()
def codex_project(tmp_path):
    """A tmp project with AGENTS.md (and no .claude/) — auto-detect → codex."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n")
    return tmp_path


def test_scaffold_claude_mode_writes_claude_md_stubs(claude_project):
    """Claude install: each scaffolded directory gets .gitkeep + CLAUDE.md."""
    payload = _scaffold(
        claude_project,
        [
            {"name": "frontend", "purpose": "UI / client-side sources", "agent": "frontend"},
            {"name": "backend",  "purpose": "API and service code",      "agent": "backend"},
        ],
    )
    assert payload["success"] is True
    assert payload["mode"] == "claude"
    assert {c["directory"] for c in payload["created"]} == {"frontend", "backend"}

    for sub in ("frontend", "backend"):
        assert (claude_project / sub / ".gitkeep").exists(), f"{sub}/.gitkeep missing"
        stub = claude_project / sub / "CLAUDE.md"
        assert stub.exists(), f"{sub}/CLAUDE.md missing"
        assert not (claude_project / sub / "AGENTS.md").exists(), (
            f"AGENTS.md should not be written in claude mode (found in {sub}/)"
        )

    body = (claude_project / "frontend" / "CLAUDE.md").read_text()
    assert "frontend/" in body
    assert "UI / client-side sources" in body
    assert "frontend" in body  # agent name


def test_scaffold_codex_mode_writes_agents_md_stubs(codex_project):
    """Codex install: each scaffolded directory gets .gitkeep + AGENTS.md (not CLAUDE.md)."""
    payload = _scaffold(
        codex_project,
        [{"name": "backend", "purpose": "API and service code", "agent": "backend"}],
    )
    assert payload["mode"] == "codex"
    assert (codex_project / "backend" / ".gitkeep").exists()
    assert (codex_project / "backend" / "AGENTS.md").exists()
    assert not (codex_project / "backend" / "CLAUDE.md").exists(), (
        "CLAUDE.md should not be written in codex mode"
    )

    body = (codex_project / "backend" / "AGENTS.md").read_text()
    assert "API and service code" in body
    assert "backend" in body


def test_scaffold_skips_directories_with_existing_content(claude_project):
    """Directories that already contain files (other than .gitkeep) must be skipped —
    /tusk-init must never overwrite user code."""
    src = claude_project / "src"
    src.mkdir()
    (src / "main.swift").write_text("// existing user code\n")

    payload = _scaffold(
        claude_project,
        [{"name": "src", "purpose": "main sources", "agent": "mobile"}],
    )
    assert payload["created"] == []
    assert len(payload["skipped"]) == 1
    assert payload["skipped"][0]["directory"] == "src"
    assert "already contains files" in payload["skipped"][0]["reason"]

    # User code preserved, no stub written
    assert (src / "main.swift").read_text() == "// existing user code\n"
    assert not (src / "CLAUDE.md").exists()


def test_scaffold_is_idempotent_on_rerun(claude_project):
    """A second invocation against the same spec must skip every directory it
    already wrote — no clobbering of stubs or .gitkeeps."""
    spec = [{"name": "docs", "purpose": "Documentation", "agent": "docs"}]
    first = _scaffold(claude_project, spec)
    assert len(first["created"]) == 1

    stub = claude_project / "docs" / "CLAUDE.md"
    original_body = stub.read_text()
    stub.write_text(original_body + "\n# user-edited content\n")
    edited_body = stub.read_text()

    second = _scaffold(claude_project, spec)
    assert second["created"] == []
    assert second["skipped"][0]["directory"] == "docs"
    # User edits to the stub are preserved on re-run
    assert stub.read_text() == edited_body


def test_scaffold_rejects_path_traversal(claude_project):
    """Directory names containing .. or absolute paths must be skipped, not created
    outside the repo root."""
    payload = _scaffold(
        claude_project,
        [
            {"name": "../escape", "purpose": "x", "agent": "x"},
            {"name": "/abs/path", "purpose": "x", "agent": "x"},
            {"name": "ok",        "purpose": "x", "agent": "x"},
        ],
    )
    created_names = {c["directory"] for c in payload["created"]}
    assert created_names == {"ok"}
    assert not (claude_project.parent / "escape").exists()


def test_scaffold_explicit_mode_overrides_autodetect(claude_project):
    """Explicit --mode codex on a .claude/ project still writes AGENTS.md stubs."""
    payload = _scaffold(
        claude_project,
        [{"name": "x", "purpose": "p", "agent": "a"}],
        mode="codex",
    )
    assert payload["mode"] == "codex"
    assert (claude_project / "x" / "AGENTS.md").exists()
    assert not (claude_project / "x" / "CLAUDE.md").exists()


def test_install_sh_runs_in_non_git_directory(tmp_path):
    """install.sh must no longer hard-fail outside a git repo (TASK-212).

    Prior behaviour: `git rev-parse --show-toplevel` failed → exit 1 with
    'Run this from a git repository root.' Now install.sh falls back to $PWD
    and proceeds. We use Claude mode (.claude/ marker) so the full install path
    runs end-to-end."""
    project = tmp_path / "fresh-project"
    project.mkdir()
    (project / ".claude").mkdir()
    assert not (project / ".git").exists(), "fixture sanity: must not be a git repo"

    result = subprocess.run(
        ["bash", INSTALL_SH],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, (
        f"install.sh failed in non-git dir:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "Run this from a git repository root" not in result.stdout
    assert "Run this from a git repository root" not in result.stderr
    # Claude-mode install drops the binary into .claude/bin/
    assert (project / ".claude" / "bin" / "tusk").exists()
    # And the .git/hooks/ dispatcher block silently skips when .git is absent
    assert "skipping git-event dispatcher install" in result.stdout
