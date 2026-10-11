"""Real Codex canonical names/payloads, isolated read-only task identity."""
import io
import json
from pathlib import Path

import pytest
from tests.unit.test_codex_task_context_hooks import pickup


@pytest.fixture
def guard(pickup, monkeypatch):
    p = pickup
    p.conn.executescript('''
        CREATE TABLE task_scope(task_id INTEGER, pattern TEXT, source TEXT);
        INSERT INTO task_scope VALUES(42, 'src/', 'operator_declared');
    ''')
    (p.primary / 'tusk').mkdir()
    (p.primary / 'tusk/config.json').write_text(json.dumps({'scope': {'always_allowed': ['VERSION']}}))
    foreign = p.primary.parent / 'foreign'
    foreign.mkdir()
    original = p.hooks.git
    def git(cwd, *args, **kwargs):
        if Path(cwd) == foreign and args == ('worktree', 'list', '--porcelain'):
            return f'worktree {foreign}'
        return original(cwd, *args)
    monkeypatch.setattr(p.hooks, 'git', git)
    for key in ('TUSK_PROJECT', 'TUSK_DB', 'TUSK_CODEX_PRETOOL'):
        monkeypatch.delenv(key, raising=False)
    def invoke(command, tool='Bash', **inputs):
        return p.invoke(hook_event_name='PreToolUse', tool_name=tool,
                        tool_input={'command': command, **inputs})
    p.foreign, p.check = foreign, invoke
    return p


def denied(result):
    output = result['hookSpecificOutput']
    assert output['hookEventName'] == 'PreToolUse'
    assert output['permissionDecision'] == 'deny'
    assert len(output['permissionDecisionReason']) < 400
    return output['permissionDecisionReason']


def patch(path, operation='Update', extra=''):
    return f'*** Begin Patch\n*** {operation} File: {path}\n{extra}@@\n-old\n+new\n*** End Patch'


def test_contract_1(guard):
    p = guard
    assert 'TUSK_PROJECT' in denied(p.check(f'cd {p.foreign} && tusk task-done 42 --reason completed'))
    assert 'TUSK_PROJECT' in denied(p.check('bin/tusk progress 42 --note checkpoint', workdir=str(p.foreign)))
    for op in ('Add', 'Update', 'Delete'):
        assert 'scope expand 42' in denied(p.check(patch('private/data.txt', op), 'apply_patch'))
    denied(p.check(patch('src/good.py', extra='*** Move to: private/bad.py\n'), 'apply_patch'))
    denied(p.check(patch('src/../../escape.py'), 'apply_patch'))
    (p.first / 'src').mkdir()
    (p.first / 'src/link').symlink_to(p.foreign, target_is_directory=True)
    denied(p.check(patch('src/link/file.py'), 'apply_patch'))
    assert p.conn.execute('SELECT status FROM tasks WHERE id=42').fetchone()[0] == 'In Progress'
    assert p.conn.execute('SELECT count(*) FROM task_scope').fetchone()[0] == 1


def test_contract_2(guard, monkeypatch):
    p = guard
    for cmd in ('tusk task-get 42', 'tusk criteria list 42', 'tusk scope list 42',
                'tusk task-done --help', 'tusk review summary 42',
                'tusk task-import --file tasks.json --dry-run'):
        assert p.check(cmd, workdir=str(p.foreign)) == {}
    for cmd in (f'TUSK_PROJECT={p.primary} tusk progress 42 --note ok',
                f'cd {p.foreign} && env TUSK_PROJECT={p.primary} tusk task-update 42 --priority High'):
        assert p.check(cmd, workdir=str(p.foreign)) == {}
    monkeypatch.setenv('TUSK_PROJECT', str(p.primary))
    assert p.check('tusk task-done 42 --reason completed', workdir=str(p.foreign)) == {}
    monkeypatch.delenv('TUSK_PROJECT')
    assert p.check('tusk progress 42 --note checkpoint', workdir=str(p.second)) == {}
    assert p.check(patch('src/ok.py'), 'apply_patch') == {}
    assert p.check(patch('VERSION'), 'apply_patch') == {}
    p.conn.execute('INSERT INTO task_sessions VALUES(44, 42, NULL)')
    p.conn.commit()
    assert p.check(patch('outside.py'), 'apply_patch') == {}
    p.conn.execute('DELETE FROM task_sessions WHERE task_id=42')
    p.conn.commit()
    assert p.check('tusk task-done 42 --reason completed', workdir=str(p.foreign)) == {}


def test_contract_3(guard, monkeypatch):
    p = guard
    nested = p.first / 'src'
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert p.invoke(cwd=nested, hook_event_name='PreToolUse', tool_name='apply_patch',
                    tool_input={'command': patch('ok.py')}) == {}
    denied(p.invoke(cwd=nested, hook_event_name='PreToolUse', tool_name='apply_patch',
                     tool_input={'command': patch('../outside.py')}))
    for tool, inputs in [('Bash', None), ('apply_patch', {'command': []}),
                         ('exec_command', {'cmd': 'tusk task-done 42'}),
                         ('mcp__files__write', {'file_path': 'outside.py'})]:
        assert p.invoke(hook_event_name='PreToolUse', tool_name=tool, tool_input=inputs) == {}
    for cmd in ('if true; then tusk task-done 42; fi', 'tusk task-done "$TASK"',
                'echo hi | tusk task-update 42', 'python -c "edit files"', 'tusk task-done "'):
        assert p.check(cmd, workdir=str(p.foreign)) == {}
    assert p.check('not a patch', 'apply_patch') == {}
    assert p.hooks.dispatch(io.StringIO('{broken')) == {}
    registration = p.hooks.definitions()['PreToolUse'][0]
    assert registration['matcher'] == '^(Bash|apply_patch)$'
    monkeypatch.setenv('TUSK_CODEX_PRETOOL', '0')
    monkeypatch.setattr(p.hooks, 'resolve_context', lambda _: pytest.fail('opt-out should skip identity'))
    assert p.check(patch('outside.py'), 'apply_patch') == {}


def test_unbounded_and_missing_scope_are_quiet(guard):
    p = guard
    p.conn.execute("INSERT INTO task_scope VALUES(42, '*', 'unbounded')")
    p.conn.commit()
    assert p.check(patch('outside.py'), 'apply_patch') == {}
    p.conn.execute('DELETE FROM task_scope')
    p.conn.commit()
    assert p.check(patch('outside.py'), 'apply_patch') == {}
