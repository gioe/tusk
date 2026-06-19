#!/usr/bin/env python3
"""Verify that the agent-sandbox permissions required by /review-commits are
declared in `.claude/settings.json`.

Two roots are involved when this runs from a git worktree subdirectory:

  - **db-derived repo root** — resolved from `db_path` (argv[0]). `bin/tusk`
    routes `DB_PATH` through `git --git-common-dir`, so from a worktree this
    points at the *primary checkout*. This is the settings file the check has
    always validated.
  - **CWD-derived project root** — the nearest `.git` ancestor of the invoking
    CWD (a worktree's `.git` is a *file*, not a directory). This is the project
    root a spawned subagent uses to resolve `permissions.allow`; it is NOT
    routed through `git-common-dir`, so in a worktree it is the *worktree root*,
    not the primary checkout (issue #1091).

When the two roots differ, a `tusk review-check-perms` OK validated against the
primary checkout's settings is meaningless for a subagent that inherits the
worktree root's (possibly absent) settings — exactly the failure in issue #1091
where the check printed OK and the very next spawned reviewer agent got a hard
Bash denial. The mismatch branch below validates the CWD-derived settings file
too and reports the file path(s) it inspected.

Settings read order (for either root):
    1. `<root>/.claude/settings.json` on disk.
    2. If missing, fall back to `git show HEAD:.claude/settings.json`
       (handles `tusk branch` stashing uncommitted changes).

Outputs (single line to stdout) + exit code:
    - OK
        → exit 0 (stderr names the validated settings file; when the CWD root
          diverges but also grants the required permissions, stderr says so)
    - MISSING: <reason or entries>   (db-derived settings absent/malformed/short)
        → exit 1
    - MISMATCH: <explanation naming both settings files>
        (CWD-derived project root differs from the db-derived root AND its
         settings file is absent/malformed or lacks a required permission —
         a spawned subagent would not inherit the validated permissions)
        → exit 2

Usage:
    tusk review-check-perms
"""

import json
import os
import subprocess
import sys

REQUIRED_PERMISSIONS = [
    "Bash(git diff:*)",
    "Bash(git remote:*)",
    "Bash(git symbolic-ref:*)",
    "Bash(git branch:*)",
    "Bash(tusk review:*)",
]


def _read_settings(root: str) -> tuple[dict | None, str | None]:
    """Load `<root>/.claude/settings.json` from disk; fall back to git show HEAD:.

    Returns ``(settings_dict, None)`` on success, or ``(None, message)`` where
    ``message`` is the ``MISSING: …`` line describing why the file could not be
    loaded. Unlike the old printing helper, this never writes to stdout — the
    caller decides whether the failure is a db-derived ``MISSING`` (exit 1) or a
    CWD-derived ``MISMATCH`` (exit 2).
    """
    path = os.path.join(root, ".claude", "settings.json")
    try:
        with open(path) as f:
            return json.load(f), None
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        return None, "MISSING: .claude/settings.json on disk is not valid JSON"

    r = subprocess.run(
        ["git", "show", "HEAD:.claude/settings.json"],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=root,
    )
    if r.returncode != 0:
        return None, (
            "MISSING: .claude/settings.json not found on disk or in HEAD — "
            "no permissions.allow configured"
        )
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return None, "MISSING: .claude/settings.json in HEAD is not valid JSON"


def _missing_permissions(settings: dict) -> str | None:
    """Return a ``MISSING: …`` message if ``settings`` does not grant every
    required permission (or has a malformed permissions shape), else ``None``."""
    perms = settings.get("permissions", {})
    if not isinstance(perms, dict):
        return "MISSING: permissions is not an object — no permissions.allow configured"
    allow = perms.get("allow", [])
    if not isinstance(allow, list):
        return "MISSING: permissions.allow is not a list — no permissions.allow configured"
    missing = [p for p in REQUIRED_PERMISSIONS if p not in allow]
    if missing:
        return "MISSING: " + ", ".join(missing)
    return None


def _find_git_root(start: str) -> str | None:
    """Walk up from ``start`` to the nearest ancestor containing ``.git`` (a
    file in a linked worktree, a directory in the primary checkout). Returns the
    directory, or ``None`` if no ``.git`` is found before the filesystem root.

    This mirrors `bin/tusk`'s ``find_repo_root`` and models how a spawned
    subagent resolves its project root: nearest ``.git`` from the CWD, WITHOUT
    routing through ``git --git-common-dir`` (so from a worktree it returns the
    worktree root, not the primary checkout).
    """
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _settings_path(root: str) -> str:
    return os.path.join(root, ".claude", "settings.json")


def _strip_missing_prefix(message: str | None) -> str:
    """Render a ``MISSING: …`` message as a bare reason for embedding inside a
    MISMATCH line (drops the leading ``MISSING: `` token)."""
    if not message:
        return "unreadable"
    prefix = "MISSING: "
    return message[len(prefix):] if message.startswith(prefix) else message


def check(repo_root: str, cwd: str | None = None) -> int:
    """Validate the db-derived settings, then (when ``cwd`` is provided) detect a
    CWD-vs-repo_root settings-resolution mismatch.

    ``cwd=None`` preserves the original behavior exactly — only the db-derived
    settings are validated and the OK/MISSING contract is unchanged. ``main()``
    always passes ``os.getcwd()`` so the CLI gets the mismatch check; callers
    (including the existing unit tests) that want the legacy single-root check
    omit ``cwd``.
    """
    settings, err = _read_settings(repo_root)
    if settings is None:
        print(err)
        return 1
    missing = _missing_permissions(settings)
    if missing is not None:
        print(missing)
        return 1

    db_settings_path = _settings_path(repo_root)

    # Mismatch detection only runs for a real CLI invocation (cwd provided) and
    # only when the db-derived root is itself a checkout (has .git) — this avoids
    # spurious mismatches when TUSK_DB pins the database outside any repo.
    if cwd is not None and os.path.exists(os.path.join(repo_root, ".git")):
        cwd_root = _find_git_root(cwd)
        if cwd_root is not None and os.path.realpath(cwd_root) != os.path.realpath(repo_root):
            cwd_settings_path = _settings_path(cwd_root)
            cwd_settings, cwd_err = _read_settings(cwd_root)
            if cwd_settings is None:
                print(
                    "MISMATCH: spawned subagents resolve project settings from "
                    f"{cwd_settings_path} (CWD project root {cwd_root}), but that "
                    f"file is unusable ({_strip_missing_prefix(cwd_err)}). The "
                    f"validated file {db_settings_path} (db project root "
                    f"{repo_root}) is NOT what a subagent will inherit — run "
                    "review from the project root that owns .claude/settings.json, "
                    "or add the required permissions to the CWD project root."
                )
                return 2
            cwd_missing = _missing_permissions(cwd_settings)
            if cwd_missing is not None:
                print(
                    "MISMATCH: spawned subagents resolve project settings from "
                    f"{cwd_settings_path} (CWD project root {cwd_root}), which "
                    f"lacks required permissions ({_strip_missing_prefix(cwd_missing)}). "
                    f"The validated file {db_settings_path} (db project root "
                    f"{repo_root}) is NOT what a subagent will inherit — run review "
                    "from the project root that owns .claude/settings.json, or add "
                    "the required permissions to the CWD project root."
                )
                return 2
            # Benign divergence: the two roots differ but BOTH grant the
            # required permissions, so a spawned subagent is still covered.
            print(
                f"validated: {db_settings_path}; CWD project root differs "
                f"({cwd_root}) — its settings {cwd_settings_path} also grant the "
                "required permissions",
                file=sys.stderr,
            )
            print("OK")
            return 0

    print(f"validated: {db_settings_path}", file=sys.stderr)
    print("OK")
    return 0


def main(argv: list) -> int:
    # argv[0] = db_path, argv[1] = config_path (config unused — this script only
    # inspects .claude/settings.json). db_path resolves the db-derived repo_root
    # independently of the caller's CWD; os.getcwd() supplies the CWD-derived
    # project root a spawned subagent would inherit (issue #1091).
    db_path = argv[0]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    return check(repo_root, cwd=os.getcwd())


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk review-check-perms", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
