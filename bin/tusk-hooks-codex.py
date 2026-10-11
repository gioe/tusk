#!/usr/bin/env python3
"""Codex lifecycle registration and bounded dispatch shared by install and upgrade.

SessionStart restores a bounded current-task snapshot. Add handlers to
HANDLERS and corresponding registrations in definitions; never infer identity
from the most recent or sole project-wide session.
"""

import argparse
import copy
import hashlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time

MARKER = "tusk-codex-lifecycle-v1"
EVENTS = {"SessionStart", "Stop", "Interrupt", "SessionEnd", "PreToolUse", "PreCompact", "PostCompact"}
CONTEXT_SOURCES = {"startup", "resume", "clear", "compact"}
GUIDANCE = ("Codex lifecycle hooks installed; active status is unknown. In Codex, open /hooks "
            "to review and trust new or changed definitions. The project .codex layer must also "
            "be trusted. Tusk never grants or bypasses hook trust.")


def git(cwd, *args, timeout=1):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        raise ValueError("not a Git workspace")
    return result.stdout.strip()


def definitions():
    # A changed implementation changes the definition hash Codex asks users to
    # review. Do not rely on Codex hashing the contents of referenced scripts.
    digest = hashlib.sha256(b"".join(
        Path(__file__).with_name(name).read_bytes()
        for name in ("tusk-hooks-codex.py", "tusk-task-brief.py", "tusk-session-stats.py",
                     "tusk-pricing-lib.py", "tusk-call-breakdown.py", "tusk-git-helpers.py")
    )).hexdigest()
    script = '''r=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
for root in "$r" "$(git worktree list --porcelain | sed -n '1s/^worktree //p')"; do
  for dir in "$root/bin" "$root/tusk/bin" "$root/.claude/bin"; do
    if [ -f "$dir/tusk-hooks-codex.py" ]; then
      exec python3 "$dir/tusk-hooks-codex.py" dispatch
    fi
  done
done
exit 0'''
    command = f"sh -c {shlex.quote(script)} {MARKER} {digest}"
    registrations = {"SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
        {"type": "command", "command": command, "timeout": 3, "additionalContextLimit": 2000}
    ]}]}
    for event in ("Stop", "Interrupt", "SessionEnd"):
        registrations[event] = [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
    registrations['PreToolUse'] = [{'matcher': '^(Bash|apply_patch)$', 'hooks': [
        {'type': 'command', 'command': command, 'timeout': 3}
    ]}]
    return registrations


def owned(handler):
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    try:
        words = shlex.split(handler.get("command", ""))
    except (ValueError, TypeError):
        return False
    return len(words) == 5 and words[:2] == ["sh", "-c"] and words[3] == MARKER


def read_config(path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("hooks.json must contain an object with a hooks object")
    for groups in data.get("hooks", {}).values():
        if not isinstance(groups, list):
            raise ValueError("hook events must contain arrays of matcher groups")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("hook matcher groups must contain a hooks array")
    return data


def merged(data):
    result = copy.deepcopy(data)
    hooks = result.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            handlers = [h for h in group["hooks"] if not owned(h)]
            if handlers == group["hooks"]:
                kept.append(group)
            elif handlers:
                kept.append({**group, "hooks": handlers})
        hooks[event] = kept
    for event, groups in definitions().items():
        hooks.setdefault(event, []).extend(groups)
    return result


def reconcile(root):
    path = Path(root) / ".codex/hooks.json"
    if path.is_symlink():
        raise ValueError("refusing to replace symlinked .codex/hooks.json; manage its target explicitly")
    data = read_config(path)  # Validate before modifying any user definitions.
    updated = merged(data)
    if updated != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tusk-hooks-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(updated, stream, indent=2)
                stream.write("\n")
            os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(GUIDANCE)
    config = path.parent / "config.toml"
    if config.is_file() and "[hooks" in config.read_text(encoding="utf-8"):
        print("Note: inline hooks in .codex/config.toml are preserved; Codex loads both sources.")


def check(root):
    path = Path(root) / ".codex/hooks.json"
    try:
        data = read_config(path)
        if data != merged(data):
            return ["missing or changed registrations; run tusk hooks install --codex"]
        if path.is_symlink():
            return ["symlinked hooks.json requires manual maintenance"]
    except (OSError, ValueError) as error:
        return [str(error)]
    return []


def resolve_context(event):
    """Read-only, exact-workspace identity. Codex and Tusk session IDs differ."""
    def budget():
        remaining = min(1.0, event.get('_deadline', time.monotonic() + 1) - time.monotonic())
        if remaining <= 0:
            raise ValueError("hook deadline reached")
        return remaining
    def workspace_git(cwd, *args):
        if '_deadline' in event:
            return git(cwd, *args, timeout=budget())
        return git(cwd, *args)
    cwd = Path(event["cwd"])
    root = Path(workspace_git(cwd, "rev-parse", "--show-toplevel"))
    # The payload cannot redirect an invocation running in another repository.
    if not os.path.samefile(root, workspace_git(Path.cwd(), "rev-parse", "--show-toplevel")):
        return None
    primary = Path(workspace_git(root, "worktree", "list", "--porcelain").splitlines()[0].removeprefix("worktree "))
    binary = next((p / "tusk" for base in (root, primary)
                   for p in (base / "bin", base / "tusk/bin", base / ".claude/bin")
                   if (p / "tusk").is_file()), None)
    if binary is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUSK_")}
    env.update(TUSK_PROJECT=str(primary), TUSK_QUIET="1")
    result = subprocess.run([str(binary), "path"], cwd=root, env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=budget())
    if result.returncode:
        return None
    db = Path(result.stdout.strip())
    # No creation, migrations, locks for writes, or inherited cross-project pins.
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        query_deadline = min(event.get('_deadline', time.monotonic() + .5), time.monotonic() + .5)
        connection.set_progress_handler(lambda: int(time.monotonic() > query_deadline), 1000)
        if event.get("hook_event_name") == "SessionEnd":
            pinned = connection.execute(
                "SELECT id, task_id FROM task_sessions WHERE transcript_provider='codex' AND transcript_path=?",
                (str(Path(event["transcript_path"]).resolve()),),
            ).fetchall()
            if pinned:
                if len(pinned) != 1:
                    return None
                return {"root": str(root), "project": str(primary), "db_path": str(db),
                        "task_id": pinned[0][1], "session_id": pinned[0][0],
                        "codex_session_id": event["session_id"]}
        rows = connection.execute("SELECT task_id, workspace_path, branch FROM task_workspaces").fetchall()
        branch = workspace_git(root, "branch", "--show-current")
        tasks = {task for task, workspace, recorded_branch in rows
                 if Path(workspace).exists() and os.path.samefile(workspace, root)
                 and recorded_branch == branch}
        if len(tasks) != 1:
            return None
        task_id = tasks.pop()
        sessions = connection.execute(
            "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NULL", (task_id,)
        ).fetchall()
        if len(sessions) != 1:
            return None
        return {"root": str(root), "project": str(primary), "db_path": str(db),
                "task_id": task_id, "session_id": sessions[0][0],
                "codex_session_id": event["session_id"]}
    finally:
        connection.close()


def task_context(event, context):
    """Hydrate without starting sessions, modifying progress or marking criteria."""
    sys.path.insert(0, str(Path(__file__).parent))
    import tusk_loader
    brief = tusk_loader.load("tusk-task-brief")
    connection = sqlite3.connect(Path(context["db_path"]).as_uri() + "?mode=ro",
                                 uri=True, timeout=0.1)
    try:
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + 0.5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        text = brief.build_hook_brief(connection, context["task_id"], context["session_id"])
        if text is None:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    finally:
        connection.close()


def claim_reminder(context, turn_id):
    """Bounded local delivery cache, separate from task data; never wait for a peer."""
    # The installer ignores this local delivery state in consumer repos.
    directory = Path(context["db_path"] + "-hook-state") / "codex-checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    # One file per Tusk session; no arbitrary event strings in file names.
    path = directory / f"{int(context['session_id'])}.json"
    key = hashlib.sha256(json.dumps([context["task_id"], context["codex_session_id"], turn_id]).encode()).hexdigest()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stream.seek(0)
        raw = stream.read(20000)
        try:
            seen = json.loads(raw) if raw else []
        except ValueError:
            return False  # Uncertain delivery history: quiet, not duplicate output.
        if not isinstance(seen, list) or any(not isinstance(item, str) or len(item) != 64 for item in seen):
            return False
        if key in seen:
            return False
        stream.seek(0)
        json.dump((seen + [key])[-128:], stream)
        stream.truncate()
        stream.flush()
    return True


def checkpoint_reminder(event, context):
    """Advisory only. Never mutate tasks, progress, criteria or session lifecycle."""
    connection = sqlite3.connect(Path(context["db_path"]).as_uri() + "?mode=ro",
                                 uri=True, timeout=0.1)
    try:
        deadline = time.monotonic() + 0.5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        sessions = connection.execute(
            "SELECT s.id, s.started_at FROM task_sessions s JOIN tasks t ON t.id=s.task_id "
            "WHERE s.task_id=? AND s.ended_at IS NULL AND t.status='In Progress' LIMIT 2",
            (context["task_id"],),
        ).fetchall()
        if len(sessions) != 1 or sessions[0][0] != context["session_id"]:
            return {}
        unfinished = connection.execute(
            "SELECT 1 FROM acceptance_criteria WHERE task_id=? AND is_completed=0 LIMIT 1",
            (context["task_id"],),
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT 1 FROM task_progress WHERE task_id=? "
            "AND trim(coalesce(next_steps, ''), char(9)||char(10)||char(11)||char(12)||char(13)||' ') <> '' "
            "AND created_at >= ? AND created_at >= datetime('now', '-30 minutes') "
            "AND created_at <= datetime('now') LIMIT 1", (context["task_id"], sessions[0][1]),
        ).fetchone()
        if not unfinished or checkpoint:
            return {}
    finally:
        connection.close()
    if not claim_reminder(context, event["turn_id"]):
        return {}
    return {"systemMessage": (
        f"TASK-{context['task_id']} has unfinished criteria and no recent recovery checkpoint. "
        f"When you resume, save useful next steps with tusk progress {context['task_id']} --next-steps. "
        "Stopping is respected; task state is unchanged."
    )}


def session_end(event, context):
    recovery = f"Tusk telemetry deferred; run tusk session-stats {context['session_id']} <transcript-path>."
    remaining = min(2.0, event.get('_deadline', time.monotonic() + 2.0) - time.monotonic())
    if remaining <= 0:
        return {"systemMessage": recovery}
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "capture"],
            input=json.dumps({"event": event, "context": context}), capture_output=True,
            text=True, encoding='utf-8', timeout=remaining,
        )
        if result.returncode:
            return {"systemMessage": recovery}
        output = json.loads(result.stdout)
        return output if isinstance(output, dict) else {"systemMessage": recovery}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"systemMessage": recovery}


def capture_worker(payload):
    sys.path.insert(0, str(Path(__file__).parent))
    import tusk_loader
    context = payload['context']
    try:
        stats = tusk_loader.load('tusk-session-stats')
        connection = sqlite3.connect(Path(context['db_path']).as_uri() + '?mode=rw',
                                     uri=True, timeout=0.1, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            deadline = time.monotonic() + 1.5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            return stats.capture_hook(connection, context, payload['event'])
        finally:
            connection.close()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {"systemMessage": f"Tusk telemetry unavailable or partial; retained recorded data. "
                f"Run tusk session-stats {context['session_id']} <transcript-path>."}


# Only classify known mutations; unknown/new commands remain the CLI's concern.
PRETOOL_WRITES = {
    'task-start', 'task-done', 'task-update', 'task-insert', 'task-import',
    'task-reopen', 'progress', 'commit', 'merge', 'branch', 'session-close',
    'session-stats', 'session-recalc', 'version-bump', 'changelog-add',
}
PRETOOL_SUBWRITES = {
    'criteria': {'add', 'done', 'skip', 'reset', 'delete'},
    'scope': {'add', 'expand', 'remove', 'lock', 'rederive'},
    'context': {'add', 'resolve', 'supersede'},
    'deps': {'add', 'remove'}, 'jot': {'write', 'promote', 'dismiss'},
    'review': {'start', 'begin', 'add-comment', 'resolve', 'approve', 'request-changes'},
    'objective': {'insert', 'update', 'link', 'unlink', 'done'},
    'task-worktree': {'create', 'prune'}, 'skill-run': {'start', 'finish', 'cancel'},
}


def deny_pretool(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def literal_shell(command, cwd):
    """A deliberately small shell subset, never evaluated or executed.

    One command, optionally prefixed by `cd <literal> &&`, env and literal
    assignments. Expansion, redirects, pipelines and compound scripts are unknown.
    """
    if any(c in command for c in '$`\n\r<>|;(){}*?~\\'):
        return None
    words = shlex.split(command)
    if len(words) >= 4 and words[0] == 'cd' and words[2] == '&&':
        cwd = (cwd / words[1]).resolve()
        words = words[3:]
    if any('&' in word for word in words):
        return None
    if words and words[0] == 'env':
        words = words[1:]
    pins = {k: os.environ.get(k, '') for k in ('TUSK_PROJECT', 'TUSK_DB')}
    while words and '=' in words[0]:
        key, value = words.pop(0).split('=', 1)
        if not key.isidentifier():
            return None
        if key in pins:
            pins[key] = value
    if not words or Path(words[0]).name != 'tusk' or len(words) < 2:
        return None
    if any(flag in words[1:] for flag in ('--help', '-h', '--dry-run')):
        return None
    verb = words[1]
    if verb not in PRETOOL_WRITES and not (len(words) > 2 and words[2] in PRETOOL_SUBWRITES.get(verb, set())):
        return None
    return cwd, pins


def patch_paths(command):
    lines = command.splitlines()
    if not lines or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        return None
    paths = []
    for line in lines[1:-1]:
        for marker in ('*** Add File: ', '*** Update File: ', '*** Delete File: ', '*** Move to: '):
            if line.startswith(marker):
                path = line[len(marker):]
                if not path.strip() or '\x00' in path:
                    return None
                paths.append(path)
                break
    return paths


def pretool(event, context):
    inputs = event['tool_input']
    command = inputs['command']
    cwd = Path(event['cwd'])
    override = inputs.get('workdir', inputs.get('cwd'))
    if override is not None:
        if not isinstance(override, str) or not Path(override).is_absolute():
            return {}
        cwd = Path(override)
    task_id = context['task_id']
    if event['tool_name'] == 'Bash':
        parsed = literal_shell(command, cwd)
        if parsed is None:
            return {}
        target, pins = parsed
        # Explicit project/DB routing expresses intent we must not infer away.
        if pins['TUSK_PROJECT'] or pins['TUSK_DB']:
            return {}
        remaining = min(1, event.get('_deadline', time.monotonic() + 1) - time.monotonic())
        if remaining <= 0:
            return {}
        primary = git(target, 'worktree', 'list', '--porcelain', timeout=remaining).splitlines()[0].removeprefix('worktree ')
        if not os.path.samefile(primary, context['project']):
            return deny_pretool(f'Tusk write targets another project from TASK-{task_id}. '
                                'Run in the owning task workspace or explicitly set TUSK_PROJECT to the intended project.')
        return {}
    paths = patch_paths(command)
    if not paths:
        return {}
    # Existing authoritative declarations only: never infer scope from chat text.
    connection = sqlite3.connect(Path(context['db_path']).as_uri() + '?mode=ro', uri=True, timeout=.1)
    try:
        deadline = time.monotonic() + .5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute('BEGIN')
        sessions = connection.execute(
            "SELECT s.id FROM task_sessions s JOIN tasks t ON t.id=s.task_id "
            "WHERE s.task_id=? AND s.ended_at IS NULL AND t.status='In Progress'", (task_id,)).fetchall()
        if sessions != [(context['session_id'],)]:
            return {}
        rows = connection.execute('SELECT pattern, source FROM task_scope WHERE task_id=?', (task_id,)).fetchall()
        if not rows or any(source == 'unbounded' for _, source in rows):
            return {}
        patterns = [pattern for pattern, _ in rows]
    finally:
        connection.close()
    config = json.loads((Path(context['project']) / 'tusk/config.json').read_text())
    if not isinstance(config, dict) or not isinstance(config.get('scope', {}), dict):
        return {}
    allowed = config.get('scope', {}).get('always_allowed', [])
    if not isinstance(allowed, list) or not all(isinstance(p, str) for p in allowed):
        return {}
    sys.path.insert(0, str(Path(__file__).parent))
    import tusk_loader
    matches = tusk_loader.load('tusk-git-helpers').path_matches_scope
    root = Path(context['root']).resolve()
    for path in paths:
        # Normalize traversal and symlinks before comparing repository-relative paths.
        absolute = (cwd / path).resolve()
        relative = str(absolute.relative_to(root)) if absolute.is_relative_to(root) else None
        if relative is None or not matches(relative, patterns + allowed):
            display = ' '.join(path.split())[:160]
            return deny_pretool(f'TASK-{task_id}: edit outside declared scope: {display}. '
                                f'Use the owning workspace or record an intentional expansion with tusk scope expand {task_id} <path> --reason <why>.')
    return {}


HANDLERS = {"SessionStart": task_context, "Stop": checkpoint_reminder,
            "Interrupt": checkpoint_reminder, "SessionEnd": session_end, "PreToolUse": pretool}


def dispatch(stream):
    deadline = time.monotonic() + 2.5
    try:
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {}
        event = json.loads(raw)
        if not isinstance(event, dict):
            return {}
        event.pop("_deadline", None)  # Internal budget is never supplied by the payload.
        name = event.get("hook_event_name")
        if not isinstance(name, str) or name not in EVENTS:
            return {}
        if any(not isinstance(event.get(key), str) or not event[key].strip()
               for key in ("cwd", "session_id")) or not Path(event["cwd"]).is_absolute():
            return {}
        if name == 'PreToolUse':
            inputs = event.get('tool_input')
            if (os.environ.get('TUSK_CODEX_PRETOOL', '1').lower() in {'0', 'false', 'off'}
                    or event.get('tool_name') not in ('Bash', 'apply_patch')
                    or not isinstance(inputs, dict) or not isinstance(inputs.get('command'), str)):
                return {}
            event['_deadline'] = deadline
        if name == "SessionStart":
            source = event.get("source")
            if (not isinstance(source, str) or source not in CONTEXT_SOURCES
                    or os.environ.get("TUSK_CODEX_CONTEXT", "1").lower() in {"0", "false", "off"}):
                return {}
        if name in {"Stop", "Interrupt"}:
            turn = event.get("turn_id")
            if (not isinstance(turn, str) or not turn.strip() or len(turn) > 256
                    or len(event["session_id"]) > 256
                    or event.get("stop_hook_active", False) is not False
                    or os.environ.get("TUSK_CODEX_CHECKPOINTS", "1").lower() in {"0", "false", "off"}):
                return {}
        if name == "SessionEnd":
            transcript = event.get('transcript_path')
            if (not isinstance(transcript, str) or not Path(transcript).is_absolute()
                    or os.environ.get('TUSK_CODEX_TELEMETRY', '1').lower() in {'0', 'false', 'off'}):
                return {}
            event['_deadline'] = deadline
        handler = HANDLERS.get(name)
        if handler is None:
            return {}
        context = resolve_context(event)
        return handler(event, context) if context else {}
    except (OSError, ValueError, ImportError, sqlite3.Error, subprocess.SubprocessError):
        return {}  # A missing DB, malformed event or ambiguous identity is a no-op.


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("install", "check", "dispatch", "capture"))
    parser.add_argument("root", nargs="?")
    args = parser.parse_args()
    if args.command == "capture":
        print(json.dumps(capture_worker(json.loads(sys.stdin.read(1024 * 1024)))))
        return 0
    if args.command == "dispatch":
        # JSON is also valid for Stop, whose stdout must not be plain text.
        print(json.dumps(dispatch(sys.stdin)))
        return 0
    if not args.root:
        parser.error("root is required for install/check")
    try:
        if args.command == "install":
            reconcile(args.root)
        else:
            issues = check(args.root)
            if issues:
                print("WARNING: Codex lifecycle hooks: " + "; ".join(issues), file=sys.stderr)
                return 1
            print(GUIDANCE)
    except (OSError, ValueError) as error:
        print(f"ERROR: Codex lifecycle hooks: {error}. Existing configuration was preserved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
