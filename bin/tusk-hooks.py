#!/usr/bin/env python3
"""Install/check Git hooks; shared by install.sh, upgrade, and tusk hooks."""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

MARKER = "TUSK_HOOK_DISPATCHER_V1"
EVENTS = {
    "pre-commit": ("block-raw-sqlite", "block-sql-neq", "dupe-gate", "scope-guard"),
    "pre-push": ("branch-naming", "version-bump-check"),
    "commit-msg": ("commit-msg-format",),
}


def hook_directory(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise ValueError("not a Git working tree; initialize Git, then run 'tusk hooks install'")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else Path(root) / path


def install_role(root, script_dir):
    marker = Path(script_dir) / "install-mode"
    value = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if value.endswith("-consumer"):
        return "consumer"
    if value.endswith("-source"):
        return "source"
    # Legacy mode-only markers do not reliably distinguish consumer repos.
    return "source" if (Path(root) / "install.sh").is_file() and (Path(root) / "hooks/git").is_dir() else "consumer"


def guards(event, role):
    return tuple(g for g in EVENTS[event] if role == "source" or g != "version-bump-check")


def guard_source(root, script_dir):
    source = Path(root) / "hooks/git"
    return source if install_role(root, script_dir) == "source" and source.is_dir() else Path(script_dir) / "hooks/git"


def guard_destination(root, script_dir):
    if Path(script_dir).resolve() == (Path(root) / "bin").resolve() and (Path(root) / "hooks/git").is_dir():
        return Path(root) / "hooks/git"
    return Path(script_dir) / "hooks/git"


def dispatcher(event, role, source):
    names = guards(event, role)
    hashes = "".join(
        f"# {g}: {hashlib.sha256((source / (g + '.sh')).read_bytes()).hexdigest()}\n"
        for g in names
    )
    return f'''#!/bin/bash
# {MARKER} — managed by tusk hooks install; do not edit.
{hashes}
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 1
HOOKS_DIR=""
find_guards() {{
  local root="$1" dir
  if [[ -f "$root/bin/tusk-hooks.py" && -d "$root/hooks/git" ]]; then
    HOOKS_DIR="$root/hooks/git"
    export PATH="$root/bin:$PATH"
    return 0
  fi
  for dir in "$root/.claude/bin" "$root/tusk/bin" "$root/bin"; do
    if [[ -d "$dir/hooks/git" ]]; then
      HOOKS_DIR="$dir/hooks/git"
      export PATH="$dir:$PATH"
      return 0
    fi
  done
}}
find_guards "$REPO_ROOT"
if [[ -z "$HOOKS_DIR" ]]; then
  # A linked checkout may not contain the ignored installed binaries.
  PRIMARY="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
  find_guards "$PRIMARY"
fi
if [[ -z "$HOOKS_DIR" ]]; then
  echo "ERROR: Tusk Git guards missing; run tusk hooks install (or tusk upgrade --force)." >&2
  exit 1
fi
INPUT_FILE=""
if [[ "{event}" == "pre-push" ]]; then
  INPUT_FILE="$(mktemp)" || exit 1
  trap 'rm -f "$INPUT_FILE"' EXIT
  cat > "$INPUT_FILE" || exit 1
fi
run_hook() {{
  if [[ -n "$INPUT_FILE" ]]; then
    "$@" < "$INPUT_FILE"
  else
    "$@"
  fi
}}
for g in {' '.join(names)}; do
  script="$HOOKS_DIR/$g.sh"
  if [[ ! -x "$script" ]]; then
    echo "ERROR: Missing/non-executable Tusk guard $g; run tusk hooks install (or tusk upgrade --force)." >&2
    exit 1
  fi
  run_hook "$script" "$@"
  rc=$?
  [[ $rc -eq 0 ]] || exit $rc
done
if [[ -x "$HERE/{event}.pre-tusk" ]]; then
  run_hook "$HERE/{event}.pre-tusk" "$@"
  exit $?
fi
exit 0
'''


def managed(path):
    if path.is_symlink() or not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return len(lines) > 1 and lines[1].startswith(f"# {MARKER}")


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, 0o755)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reconcile(root, script_dir, source=None, role=None):
    root, script_dir = Path(root), Path(script_dir)
    source = Path(source) if source else guard_source(root, script_dir)
    role = role or install_role(root, script_dir)
    # Read and validate the complete release before changing any installed hook.
    assets = {g: (source / (g + ".sh")).read_bytes() for gs in EVENTS.values() for g in gs}
    rendered = {e: dispatcher(e, role, source) for e in EVENTS}
    try:
        directory = hook_directory(root)
    except ValueError:
        directory = None  # Install before git init is supported; keep the assets.
    if directory:
        for event in EVENTS:
            target = directory / event
            backup = directory / (event + ".pre-tusk")
            if os.path.lexists(target) and not managed(target):
                if target.is_dir() or os.path.lexists(backup):
                    raise ValueError(f"cannot preserve {target}: resolve the existing hook/backup collision before 'tusk hooks install'")
    for name, content in assets.items():
        atomic_write(guard_destination(root, script_dir) / (name + ".sh"), content)
    if directory is None:
        print("WARNING: Git hooks inactive until Git is initialized; then run 'tusk hooks install'.", file=sys.stderr)
        return
    directory.mkdir(parents=True, exist_ok=True)
    for event, content in rendered.items():
        target = directory / event
        if os.path.lexists(target) and not managed(target):
            target.rename(directory / (event + ".pre-tusk"))
        atomic_write(target, content.encode("utf-8"))
        print(f"  Installed {target}")


def check(root, script_dir):
    issues = []
    try:
        directory = hook_directory(root)
        source = guard_source(root, script_dir)
        role = install_role(root, script_dir)
        for event in EVENTS:
            target = directory / event
            for name in guards(event, role):
                guard = guard_destination(root, script_dir) / (name + ".sh")
                if not guard.is_file() or not os.access(guard, os.X_OK):
                    issues.append(f"missing/non-executable guard {name}")
                elif (source / guard.name).is_file() and guard.read_bytes() != (source / guard.name).read_bytes():
                    issues.append(f"stale guard {name}")
            try:
                expected = dispatcher(event, role, source)
                if target.is_symlink() or target.read_text(encoding="utf-8") != expected or not os.access(target, os.X_OK):
                    issues.append(f"missing/stale dispatcher {event}")
            except (OSError, UnicodeError):
                issues.append(f"missing/stale dispatcher or guard for {event}")
    except (OSError, ValueError) as error:
        issues.append(str(error))
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("root")
    parser.add_argument("script_dir")
    parser.add_argument("command", choices=("install", "check"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--role", choices=("source", "consumer"))
    parser.add_argument("--codex", action="store_true", help="install/check Codex lifecycle registrations instead of Git hooks")
    args = parser.parse_args()
    if args.codex:
        return subprocess.run([sys.executable, str(Path(__file__).with_name("tusk-hooks-codex.py")),
                               args.command, args.root], check=False).returncode
    try:
        if args.command == "install":
            reconcile(args.root, args.script_dir, args.source, args.role)
        else:
            issues = check(args.root, args.script_dir)
            if issues:
                print("WARNING: Tusk Git hooks: " + "; ".join(issues) + ". Repair with 'tusk hooks install'; if guard assets are missing, run 'tusk upgrade --force'.", file=sys.stderr)
                return 1
            print("Git hooks OK.")
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
