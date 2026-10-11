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
    assert data["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
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
        event = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(cwd), "session_id": "thr_codex"}
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
    event = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(linked), "session_id": "thr_codex"}
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


def test_installed_context_restoration(tmp_path, monkeypatch):
    """Execute the generated command with actual installed code and worktrees."""
    monkeypatch.delenv('TUSK_CODEX_CONTEXT', raising=False)
    init(tmp_path)
    script_dir = install(tmp_path)
    git(tmp_path, 'add', 'AGENTS.md')
    git(tmp_path, 'commit', '-m', 'initial')
    workspaces = [tmp_path.parent / f'{tmp_path.name}-task-{n}' for n in (42, 99)]
    db = run(tmp_path, str(script_dir / 'tusk'), 'path').stdout.strip()
    with sqlite3.connect(db) as conn:
        for task, workspace in zip((42, 99), workspaces):
            branch = f'feature/TASK-{task}-context'
            git(tmp_path, 'worktree', 'add', '-b', branch, str(workspace))
            conn.execute("INSERT INTO tasks(id, summary, description, status) VALUES (?, ?, ?, 'In Progress')",
                         (task, f'Context for {task}', f'Intent for {task}'))
            conn.execute('INSERT INTO task_sessions(task_id, started_at) VALUES (?, datetime(\'now\'))', (task,))
            conn.execute('INSERT INTO task_workspaces(task_id, workspace_path, branch) VALUES (?, ?, ?)',
                         (task, str(workspace), branch))
            conn.execute('INSERT INTO acceptance_criteria(task_id, criterion) VALUES (?, ?)',
                         (task, f'Promise for {task}'))
            conn.execute('INSERT INTO task_progress(task_id, next_steps) VALUES (?, ?)',
                         (task, f'Continue {task}'))
    command = json.loads((tmp_path / '.codex/hooks.json').read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
    for task, workspace in zip((42, 99), workspaces):
        nested = workspace / 'nested'
        nested.mkdir()
        for source in ('startup', 'resume', 'compact'):
            event = {'hook_event_name': 'SessionStart', 'source': source,
                     'cwd': str(nested), 'session_id': f'thr_{task}'}
            result = run(nested, 'sh', '-c', command, input=json.dumps(event))
            context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
            assert f'TASK-{task}' in context and f'Continue {task}' in context
            assert f'Promise for {task}' in context
            assert f'TASK-{99 if task == 42 else 42}' not in context
            assert result.stderr == ''
    registrations = json.loads((tmp_path / '.codex/hooks.json').read_text())['hooks']
    stop = registrations['Stop'][-1]['hooks'][0]['command']
    interrupt = registrations['Interrupt'][-1]['hooks'][0]['command']
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE task_progress SET next_steps='' WHERE task_id=42")
    event = {'hook_event_name': 'Stop', 'cwd': str(workspaces[0]), 'session_id': 'thr_42', 'turn_id': 'turn_1'}
    first = run(workspaces[0], 'sh', '-c', stop, input=json.dumps(event))
    assert set(json.loads(first.stdout)) == {'systemMessage'}
    assert 'TASK-42' in json.loads(first.stdout)['systemMessage']
    cache = Path(db + '-hook-state') / 'codex-checkpoints'
    assert cache.is_dir()
    git(tmp_path, 'check-ignore', str(next(cache.glob('*.json'))))
    event['hook_event_name'] = 'Interrupt'
    assert json.loads(run(workspaces[0], 'sh', '-c', interrupt, input=json.dumps(event)).stdout) == {}
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM tasks WHERE id=42').fetchone()[0] == 'In Progress'
        assert conn.execute('SELECT is_completed FROM acceptance_criteria WHERE task_id=42').fetchone()[0] == 0
    # Installed SessionEnd worker captures exactly once, including late delivery.
    from datetime import datetime, timezone
    monkeypatch.delenv('TUSK_CODEX_TELEMETRY', raising=False)
    monkeypatch.setenv('CODEX_THREAD_ID', 'thr_42')
    transcript = workspaces[0] / 'rollout.jsonl'
    entries = [
        {'type': 'session_meta', 'payload': {'id': 'thr_42', 'cwd': str(workspaces[0])}},
        {'type': 'turn_context', 'payload': {'model': 'gpt-6-sol'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c1'}},
        {'type': 'event_msg', 'timestamp': datetime.now(timezone.utc).isoformat(),
         'payload': {'type': 'token_count', 'info': {'last_token_usage': {
             'input_tokens': 1000, 'cached_input_tokens': 100, 'output_tokens': 20}}}},
    ]
    transcript.write_text(''.join(json.dumps(e) + '\n' for e in entries))
    end_command = registrations['SessionEnd'][-1]['hooks'][0]['command']
    event = {'hook_event_name': 'SessionEnd', 'cwd': str(workspaces[0]),
             'session_id': 'thr_42', 'transcript_path': str(transcript)}
    for _ in range(2):
        result = run(workspaces[0], 'sh', '-c', end_command, input=json.dumps(event))
        assert json.loads(result.stdout) == {}, result.stdout + result.stderr
    with sqlite3.connect(db) as conn:
        session, tokens, cost = conn.execute('SELECT id, tokens_in, cost_dollars FROM task_sessions WHERE task_id=42').fetchone()
        assert tokens == 1000 and cost > 0
        assert conn.execute('SELECT sum(call_count) FROM tool_call_stats WHERE session_id=?', (session,)).fetchone()[0] == 1
        conn.execute("UPDATE task_sessions SET ended_at=datetime('now', '+1 second') WHERE id=?", (session,))
        conn.execute("INSERT INTO task_sessions(task_id, started_at) VALUES (42, datetime('now'))")
    assert json.loads(run(workspaces[0], 'sh', '-c', end_command, input=json.dumps(event)).stdout) == {}
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT tokens_in FROM task_sessions WHERE task_id=42 AND id != ?', (session,)).fetchone()[0] is None
    # A missed hook remains recoverable by the regular CLI, even above hook size limits.
    transcript.write_text(transcript.read_text() + json.dumps({'type': 'unrelated', 'payload': {'text': 'x' * (2 * 1024 * 1024)}}) + '\n')
    assert 'budget' in json.loads(run(workspaces[0], 'sh', '-c', end_command, input=json.dumps(event)).stdout)['systemMessage']
    run(workspaces[0], str(script_dir / 'tusk'), 'session-stats', str(session), str(transcript))
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT tokens_in FROM task_sessions WHERE id=?', (session,)).fetchone()[0] == 1000
        assert conn.execute('SELECT status FROM tasks WHERE id=42').fetchone()[0] == 'In Progress'
        assert conn.execute('SELECT is_completed FROM acceptance_criteria WHERE task_id=42').fetchone()[0] == 0
    # Installing an updated brief changes the definition requiring trust review.
    renderer = script_dir / 'tusk-task-brief.py'
    renderer.write_text(renderer.read_text() + '\n# Context renderer release change\n')
    result = run(tmp_path, str(script_dir / 'tusk'), 'hooks', 'check', '--codex', check=False)
    assert result.returncode == 1 and 'changed registrations' in result.stderr
