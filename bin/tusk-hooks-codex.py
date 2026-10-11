#!/usr/bin/env python3
"""Codex lifecycle registration and bounded dispatch shared by install and upgrade.

SessionStart restores a bounded current-task snapshot. Add handlers to
HANDLERS and corresponding registrations in definitions; never infer identity
from the most recent or sole project-wide session.
"""

import argparse
import copy
import hashlib
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


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, encoding="utf-8", timeout=1)
    if result.returncode:
        raise ValueError("not a Git workspace")
    return result.stdout.strip()


def definitions():
    # A changed implementation changes the definition hash Codex asks users to
    # review. Do not rely on Codex hashing the contents of referenced scripts.
    digest = hashlib.sha256(b"".join(
        Path(__file__).with_name(name).read_bytes()
        for name in ("tusk-hooks-codex.py", "tusk-task-brief.py")
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
    return {"SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
        {"type": "command", "command": command, "timeout": 3, "additionalContextLimit": 2000}
    ]}]}


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
    cwd = Path(event["cwd"])
    root = Path(git(cwd, "rev-parse", "--show-toplevel"))
    # The payload cannot redirect an invocation running in another repository.
    if not os.path.samefile(root, git(Path.cwd(), "rev-parse", "--show-toplevel")):
        return None
    primary = Path(git(root, "worktree", "list", "--porcelain").splitlines()[0].removeprefix("worktree "))
    binary = next((p / "tusk" for base in (root, primary)
                   for p in (base / "bin", base / "tusk/bin", base / ".claude/bin")
                   if (p / "tusk").is_file()), None)
    if binary is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUSK_")}
    env.update(TUSK_PROJECT=str(primary), TUSK_QUIET="1")
    result = subprocess.run([str(binary), "path"], cwd=root, env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=1)
    if result.returncode:
        return None
    db = Path(result.stdout.strip())
    # No creation, migrations, locks for writes, or inherited cross-project pins.
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        rows = connection.execute("SELECT task_id, workspace_path, branch FROM task_workspaces").fetchall()
        branch = git(root, "branch", "--show-current")
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


HANDLERS = {"SessionStart": task_context}


def dispatch(stream):
    try:
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {}
        event = json.loads(raw)
        if not isinstance(event, dict):
            return {}
        name = event.get("hook_event_name")
        if not isinstance(name, str) or name not in EVENTS:
            return {}
        if any(not isinstance(event.get(key), str) or not event[key].strip()
               for key in ("cwd", "session_id")) or not Path(event["cwd"]).is_absolute():
            return {}
        if name == "SessionStart":
            source = event.get("source")
            if (not isinstance(source, str) or source not in CONTEXT_SOURCES
                    or os.environ.get("TUSK_CODEX_CONTEXT", "1").lower() in {"0", "false", "off"}):
                return {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {}
        context = resolve_context(event)
        return handler(event, context) if context else {}
    except (OSError, ValueError, ImportError, sqlite3.Error, subprocess.SubprocessError):
        return {}  # A missing DB, malformed event or ambiguous identity is a no-op.


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("install", "check", "dispatch"))
    parser.add_argument("root", nargs="?")
    args = parser.parse_args()
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
