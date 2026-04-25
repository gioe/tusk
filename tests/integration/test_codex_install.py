"""Integration tests for Codex-mode install.sh and tusk init (TASK-136).

Verifies that install.sh auto-detects the agent kind (Claude vs Codex) and
installs to the right layout:

- Claude mode (.claude/ present)     → install to .claude/bin/, copy skills/hooks
- Codex mode  (AGENTS.md present)    → install to tusk/bin/, no skills/hooks
- Neither present                    → hard error with a helpful message

Also checks that bin/tusk (invoked through the installed binary) detects
the install-mode marker and appends the tusk task-tool guidance to AGENTS.md
rather than CLAUDE.md in Codex mode.
"""

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
SENTINEL = "<!-- tusk-task-tools -->"


def _git_init(path):
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)


def _run_install(target_path, check=True):
    result = subprocess.run(
        ["bash", INSTALL_SH],
        cwd=str(target_path),
        capture_output=True,
        text=True,
    )
    if check:
        assert result.returncode == 0, (
            f"install.sh failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
    return result


@pytest.fixture()
def codex_project(tmp_path):
    """A tmp git repo with AGENTS.md and NO .claude/ — a pure Codex layout."""
    _git_init(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n\nExisting content.\n")
    return tmp_path


@pytest.fixture()
def bare_project(tmp_path):
    """A tmp git repo with neither .claude/ nor AGENTS.md — install should refuse."""
    _git_init(tmp_path)
    return tmp_path


def test_install_refuses_without_claude_or_agents(bare_project):
    """install.sh must error clearly when neither .claude/ nor AGENTS.md exists."""
    result = _run_install(bare_project, check=False)
    assert result.returncode != 0, "install.sh should reject projects with no agent scaffolding"
    combined = result.stdout + result.stderr
    assert ".claude/" in combined and "AGENTS.md" in combined, (
        "Error message should mention both supported agent-kind markers"
    )


def test_codex_install_lands_in_tusk_bin(codex_project):
    """Codex-mode install.sh writes binaries to tusk/bin/, not .claude/bin/."""
    _run_install(codex_project)
    tusk_bin = codex_project / "tusk" / "bin" / "tusk"
    assert tusk_bin.exists(), "tusk binary should land in tusk/bin/ for Codex projects"
    assert os.access(str(tusk_bin), os.X_OK), "tusk binary should be executable"
    # A few support files that must accompany the binary.
    for name in ["tusk_loader.py", "config.default.json", "VERSION", "pricing.json"]:
        assert (codex_project / "tusk" / "bin" / name).exists(), f"{name} missing from tusk/bin/"
    # No .claude/ should be created in a Codex project.
    assert not (codex_project / ".claude").exists(), (
        "install.sh must not create .claude/ in a Codex-only project"
    )


def test_codex_install_stamps_marker(codex_project):
    """install-mode marker is written so tusk/tusk-upgrade know which mode to apply."""
    _run_install(codex_project)
    marker = codex_project / "tusk" / "bin" / "install-mode"
    assert marker.exists(), "install-mode marker must be stamped by install.sh"
    # Marker is the compound form '<mode>-<role>'. Running install.sh from
    # tmp_path means SCRIPT_DIR != REPO_ROOT, i.e. consumer role.
    assert marker.read_text().strip() == "codex-consumer"


def test_codex_install_skips_skills_and_hooks(codex_project):
    """Codex mode has no skills/hooks primitives — install.sh must skip both."""
    _run_install(codex_project)
    assert not (codex_project / ".claude" / "skills").exists(), (
        "Codex mode should not create .claude/skills/"
    )
    assert not (codex_project / ".claude" / "hooks").exists(), (
        "Codex mode should not create .claude/hooks/"
    )
    assert not (codex_project / ".claude" / "settings.json").exists(), (
        "Codex mode should not create .claude/settings.json"
    )


def test_codex_install_writes_manifest_under_tusk(codex_project):
    """Manifest lives at tusk/tusk-manifest.json in Codex mode and references tusk/bin/ paths."""
    _run_install(codex_project)
    manifest_path = codex_project / "tusk" / "tusk-manifest.json"
    assert manifest_path.exists(), "Codex mode must write tusk/tusk-manifest.json"
    with open(manifest_path) as f:
        entries = json.load(f)
    assert any(e.startswith("tusk/bin/tusk") for e in entries), (
        "Manifest should reference tusk/bin/ paths"
    )
    assert not any(e.startswith(".claude/") for e in entries), (
        "Codex-mode manifest must not contain any .claude/ paths"
    )


def test_codex_install_updates_agents_md(codex_project):
    """tusk init (invoked by install.sh) appends guidance to AGENTS.md in codex mode."""
    _run_install(codex_project)
    agents_md = codex_project / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    assert content.startswith("# Agent Instructions"), (
        "Original AGENTS.md content should be preserved"
    )
    assert SENTINEL in content, "Tusk task-tool sentinel should be appended to AGENTS.md"
    assert "tusk task-list" in content
    # CLAUDE.md should NOT be created in a codex-only project.
    assert not (codex_project / "CLAUDE.md").exists(), (
        "Codex-mode tusk init must not create CLAUDE.md"
    )


def test_codex_install_is_idempotent(codex_project):
    """Running install.sh twice does not duplicate the AGENTS.md guidance block."""
    _run_install(codex_project)
    _run_install(codex_project)
    agents_md = codex_project / "AGENTS.md"
    assert agents_md.read_text().count(SENTINEL) == 1, (
        "Second install.sh run must not duplicate the AGENTS.md sentinel"
    )


def test_codex_install_updates_gitignore_with_tusk_bin(codex_project):
    """Codex-mode gitignore entries reference tusk/bin/, not .claude/bin/."""
    _run_install(codex_project)
    gitignore = (codex_project / ".gitignore").read_text()
    assert "tusk/bin/" in gitignore
    assert "tusk/tusk-manifest.json" in gitignore
    assert ".claude/bin/" not in gitignore, (
        "Codex-mode .gitignore must not reference .claude/ paths"
    )


def test_codex_install_copies_prompts(codex_project):
    """Codex-mode install.sh copies codex-prompts/*.md to .codex/prompts/<name>.md."""
    result = _run_install(codex_project)
    prompts_dir = codex_project / ".codex" / "prompts"
    assert prompts_dir.is_dir(), ".codex/prompts/ should be created in Codex mode"

    expected_prompts = ["tusk-init.md", "create-task.md"]
    for prompt_name in expected_prompts:
        prompt_file = prompts_dir / prompt_name
        assert prompt_file.is_file(), f"{prompt_name} missing from .codex/prompts/"
        assert prompt_file.stat().st_size > 0, f"{prompt_name} is empty"
        # Each install must announce the file in summary output.
        assert f".codex/prompts/{prompt_name}" in result.stdout, (
            f"install.sh should announce installed .codex/prompts/{prompt_name}; "
            f"stdout was:\n{result.stdout}"
        )


def test_codex_install_writes_prompts_to_manifest(codex_project):
    """tusk/tusk-manifest.json includes .codex/prompts/*.md entries in codex mode."""
    _run_install(codex_project)
    manifest_path = codex_project / "tusk" / "tusk-manifest.json"
    entries = json.loads(manifest_path.read_text())
    assert ".codex/prompts/tusk-init.md" in entries
    assert ".codex/prompts/create-task.md" in entries


@pytest.fixture()
def claude_project(tmp_path):
    """A tmp git repo with .claude/ — a Claude-only layout."""
    _git_init(tmp_path)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def test_claude_install_skips_codex_prompts(claude_project):
    """Claude-mode install.sh must not create .codex/prompts/ — it's a Codex primitive."""
    _run_install(claude_project)
    assert not (claude_project / ".codex").exists(), (
        "Claude mode should not create .codex/ in a Claude-only project"
    )
