"""Codex lifecycle installation, runtime routing, and explicit trust contracts."""
import io
import json
from pathlib import Path
import sqlite3

import pytest

from tests.integration.test_git_hook_lifecycle import ROOT, git, init, install, load, run


@pytest.mark.parametrize("mode", ["codex", "dual"])
def test_contract_1(tmp_path, monkeypatch, mode):
    repo = tmp_path / "project with spaces"
    init(repo, mode)
    config = repo / ".codex"
    config.mkdir()
    user = {"type": "command", "command": "echo user-hook"}
    original = {"description": "My hooks", "custom": {"keep": True}, "hooks": {
        "SessionStart": [{"matcher": "resume", "hooks": [user]}],
        "Stop": [{"hooks": [user]}]}}
    path = config / "hooks.json"
    path.write_text(json.dumps(original))
    inline = '[hooks]\n# User managed inline config\n'
    (config / "config.toml").write_text(inline)
    script_dir = install(repo)
    first = path.read_bytes()
    install(repo)
    assert path.read_bytes() == first
    data = json.loads(first)
    assert data["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert data["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    assert data["description"] == "My hooks" and data["custom"] == {"keep": True}
    assert len(data["hooks"]["SessionStart"]) == 2
    upgrade = load("tusk-upgrade")
    monkeypatch.chdir(repo)
    staging = tmp_path / "staging"
    staging.mkdir()
    for _ in range(2):
        data = json.loads(path.read_text())
        # Mixed ownership in a group: preserve the user's handler on repair.
        data["hooks"]["SessionStart"][-1]["hooks"].append(user)
        path.write_text(json.dumps(data))
        assert upgrade._installed_codex_hooks_stale(str(repo), str(script_dir))
        upgrade._run_upgrade_steps(str(ROOT), str(repo), str(script_dir), str(staging))
        assert not upgrade._installed_codex_hooks_stale(str(repo), str(script_dir))
        repaired = json.loads(path.read_text())
        assert repaired["hooks"]["SessionStart"][-2]["hooks"] == [user]
        assert (config / "config.toml").read_text() == inline
        stable = path.read_bytes()
        run(repo, str(script_dir / "tusk"), "hooks", "install", "--codex")
        assert path.read_bytes() == stable
    # Never replace malformed user config, even during a requested repair.
    path.write_text('{"hooks": []}')
    result = run(repo, str(script_dir / "tusk"), "hooks", "install", "--codex", check=False)
    assert result.returncode and "preserved" in result.stderr
    assert path.read_text() == '{"hooks": []}'


def test_contract_2(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    init(repo)
    script_dir = install(repo)
    command = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    nested = repo / "nested"
    nested.mkdir()
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-m", "initial")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature/TASK-42-linked", str(linked))
    for cwd in (nested, linked):
        event = {"hook_event_name": "SessionStart", "cwd": str(cwd), "session_id": "thr_codex"}
        for raw in (json.dumps(event), "{", "[]", '{"hook_event_name": []}', "x" * (1024 * 1024 + 1)):
            result = run(cwd, "sh", "-c", command, input=raw)
            assert json.loads(result.stdout) == {} and result.stderr == ""
    helper = load("tusk-hooks-codex")
    # Stub only the wrapper's path command; use real git and SQLite workspace state.
    db = tmp_path / "identity.db"
    conn = sqlite3.connect(db)
    conn.executescript('CREATE TABLE task_workspaces(task_id, workspace_path, branch);'
                       'CREATE TABLE task_sessions(id, task_id, ended_at);')
    conn.execute("INSERT INTO task_sessions VALUES (99, 99, NULL)")
    conn.commit()
    binary = script_dir / "tusk"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "' + str(db) + '"\n')
    monkeypatch.chdir(linked)
    event = {"hook_event_name": "SessionStart", "cwd": str(linked), "session_id": "thr_codex"}
    assert helper.resolve_context(event) is None  # Sole unrelated session is not a fallback.
    conn.execute("INSERT INTO task_workspaces VALUES (42, ?, ?)", (str(linked), "feature/TASK-42-linked"))
    conn.execute("INSERT INTO task_sessions VALUES (43, 42, NULL)")
    conn.commit()
    context = helper.resolve_context(event)
    assert context["task_id"] == 42 and context["session_id"] == 43
    assert context["codex_session_id"] == "thr_codex"
    helper.HANDLERS["SessionStart"] = lambda event, ctx: {"systemMessage": str(ctx["task_id"])}
    assert helper.dispatch(io.StringIO(json.dumps(event))) == {"systemMessage": "42"}
    conn.execute("INSERT INTO task_sessions VALUES (44, 42, NULL)")
    conn.commit()
    assert helper.dispatch(io.StringIO(json.dumps(event))) == {}
    # Wrong cwd cannot choose a different repository's session.
    monkeypatch.chdir(repo)
    assert helper.resolve_context(event) is None
    conn.close()


def test_contract_3(tmp_path):
    repo = tmp_path / "project"
    init(repo)
    result = run(repo, "bash", str(ROOT / "install.sh"))
    assert "active status is unknown" in result.stdout and "/hooks" in result.stdout
    binary = repo / "tusk/bin/tusk"
    for args in (("hooks", "check", "--codex"), ("validate",)):
        result = run(repo, str(binary), *args)
        assert "active status is unknown" in result.stdout
        assert "review and trust new or changed" in result.stdout
        assert "never grants or bypasses" in result.stdout
    hooks = repo / ".codex/hooks.json"
    data = json.loads(hooks.read_text())
    data["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 10
    hooks.write_text(json.dumps(data))
    result = run(repo, str(binary), "hooks", "check", "--codex", check=False)
    assert result.returncode and "missing or changed" in result.stderr
    run(repo, str(binary), "hooks", "install", "--codex")
    assert "trust" not in json.dumps(json.loads(hooks.read_text()))
    assert not (repo / ".codex/config.toml").exists()
    # Script changes must alter the definition presented to Codex for review.
    before = hooks.read_bytes()
    dispatcher = repo / "tusk/bin/tusk-hooks-codex.py"
    dispatcher.write_text(dispatcher.read_text() + "\n# Updated release\n")
    assert run(repo, str(binary), "hooks", "check", "--codex", check=False).returncode
    run(repo, str(binary), "hooks", "install", "--codex")
    assert hooks.read_bytes() != before
    # Do not follow user symlinks and mutate configuration outside this project.
    target = tmp_path / "personal-hooks.json"
    hooks.rename(target)
    original = target.read_bytes()
    hooks.symlink_to(target)
    result = run(repo, str(binary), "hooks", "install", "--codex", check=False)
    assert result.returncode and "symlinked" in result.stderr
    assert target.read_bytes() == original and hooks.is_symlink()


def test_same_version_upgrade_repairs_codex_registration(tmp_path, monkeypatch):
    import shutil
    import sys
    init(tmp_path)
    script_dir = install(tmp_path)
    (tmp_path / ".codex/hooks.json").unlink()
    upgrade = load("tusk-upgrade")
    version = int((ROOT / "VERSION").read_text())
    monkeypatch.setattr(upgrade, "get_latest_tag", lambda: f"v{version}")
    monkeypatch.setattr(upgrade, "get_remote_version", lambda _: version)
    release = tmp_path / "download/tusk-release"
    def ignore(directory, names):
        ignored = set(shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache")(directory, names))
        if Path(directory) == ROOT:
            ignored.add("tusk")
        return ignored
    shutil.copytree(ROOT, release, ignore=ignore, symlinks=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(ROOT / "bin/tusk-upgrade.py"), str(tmp_path),
                                     str(script_dir), "--no-commit", "--_rexec-src", str(release)])
    upgrade.main()
    assert not upgrade._installed_codex_hooks_stale(str(tmp_path), str(script_dir))
