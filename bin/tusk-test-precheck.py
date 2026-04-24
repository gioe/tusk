#!/usr/bin/env python3
"""Safely check whether a test failure is pre-existing (present against HEAD).

Called by the tusk wrapper:
    tusk test-precheck [--command <cmd>]

The skill's "pre-existing failure check" flow (after `tusk commit` fails with
exit code 2) previously ran ``git stash && <test_command>; git stash pop``.
When the working tree is clean, ``git stash`` becomes a no-op but
``git stash pop`` still pops whatever stale entry is on top of the stash
stack — silently trashing unrelated state.  During TASK-53 that sequence
restored an ancient stash that overwrote tusk/tasks.db, rewinding the live
DB past the current task.

This CLI wraps the logic safely:
  - If the working tree is clean, run the test directly (no git stash at all).
  - If dirty, stash with a unique named reference, run the test, then pop
    *that reference by name* — never by stash position.

Resolution order for the test command:
  1. ``--command <cmd>`` argument
  2. ``config["test_command"]``
  3. ``tusk test-detect`` result (when confidence != "none")

Output JSON (stdout):
    {
        "pre_existing": bool,   # did the test fail against HEAD (no local changes)?
        "exit_code": int,       # raw exit code returned by the test command
        "test_command": str,    # the command that was actually executed
        "stashed": bool         # did we create and pop a stash entry?
    }

Exit codes:
    0 — success (the CLI ran to completion; the test result is in the JSON)
    1 — error (no test command resolvable, git unavailable, stash push/pop failed, etc.)
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import uuid


def _run(cmd_args, cwd, capture=True):
    """Run a command list in ``cwd`` and return the CompletedProcess."""
    return subprocess.run(
        cmd_args,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )


def detect_dirty(repo_root: str) -> bool:
    """Return True if index/working-tree has any changes, including untracked."""
    result = _run(["git", "status", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def find_stash_ref_by_message(repo_root: str, message: str) -> str:
    """Return the stash ref (e.g. ``stash@{2}``) whose subject contains ``message``.

    Returns an empty string only when ``git stash list`` succeeded and no
    entry matches.  Raises ``RuntimeError`` if the command itself failed —
    callers must never conflate "command failed" with "no match", because
    doing so is what reintroduces the silent-data-loss failure mode this
    CLI exists to prevent.
    """
    result = _run(
        ["git", "stash", "list", "--format=%gd %gs"],
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git stash list failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        ref, _, subject = line.partition(" ")
        if message in subject:
            return ref
    return ""


def _normalize_path_for_match(path: str, repo_root: str) -> str:
    """Return ``path`` as a forward-slash, repo-root-relative string.

    Patterns in path_test_commands are authored as repo-root-relative globs
    (``apps/scraper/*``), but callers may pass absolute paths via --paths or
    ``git diff --name-only`` output.  Without normalization an absolute input
    like ``/Users/.../repo/apps/scraper/foo.py`` would never match
    ``apps/scraper/*``.
    """
    p = path.replace(os.sep, "/")
    if repo_root and os.path.isabs(path):
        root = repo_root.replace(os.sep, "/").rstrip("/") + "/"
        if sys.platform == "darwin":
            if p.lower().startswith(root.lower()):
                p = p[len(root):]
        else:
            if p.startswith(root):
                p = p[len(root):]
    return p


def _match_path_test_command(patterns: dict, paths, repo_root: str = "") -> str:
    """Return the first path_test_commands entry whose pattern matches every path.

    Mirrors ``match_path_test_command`` in ``tusk-commit.py`` so that the
    precheck flow picks the same subtree-scoped command when the caller has
    already told us (via --paths, or we detect via ``git diff --name-only HEAD``)
    which paths the uncommitted work touches.  Absolute paths are normalized
    to repo-root-relative form via ``_normalize_path_for_match`` before
    matching so callers can pass whatever form ``--paths`` received.  An
    empty-string command value disables that pattern — resolution falls
    through to the next entry.
    """
    if not patterns or not paths:
        return ""
    normalized = [_normalize_path_for_match(p, repo_root) for p in paths]
    for pattern, cmd in patterns.items():
        if not cmd or not isinstance(cmd, str):
            continue
        if all(fnmatch.fnmatchcase(p, pattern) for p in normalized):
            return cmd
    return ""


def _detect_changed_paths(repo_root: str) -> list:
    """Return repo-root-relative paths of changed + untracked files (best-effort).

    Used when the caller hasn't passed --paths explicitly.  Includes both
    modifications and deletions reported by ``git diff --name-only HEAD``
    (deletions surface naturally in that output, and a user who deleted only
    ``apps/scraper/foo.py`` should still resolve to the scraper-subtree
    command).  A failure to list paths downgrades path_test_commands to "no
    match" — the resolver then falls through to config["test_command"] /
    tusk test-detect, matching the pre-existing behavior.
    """
    paths: list = []
    diff = _run(["git", "diff", "--name-only", "HEAD"], cwd=repo_root)
    if diff.returncode == 0 and diff.stdout:
        paths.extend(p for p in diff.stdout.splitlines() if p)
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=repo_root,
    )
    if untracked.returncode == 0 and untracked.stdout:
        paths.extend(p for p in untracked.stdout.splitlines() if p)
    # Preserve order while de-duplicating (paths may appear in both listings).
    seen = set()
    deduped = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def resolve_test_command(explicit: str, config_path: str, repo_root: str,
                         script_dir: str, paths=None) -> str:
    """Resolve the test command.

    Resolution order:
      1. ``--command <cmd>`` (explicit override).
      2. ``path_test_commands`` — first pattern where every path in ``paths``
         matches.  When ``paths`` is None, we auto-detect from
         ``git diff --name-only HEAD`` + untracked files so the precheck flow
         lines up with the commit-time resolver without callers needing to
         replay the path list.
      3. ``config["test_command"]`` (global).
      4. ``tusk test-detect`` when confidence is not "none".
    """
    if explicit:
        return explicit

    cfg = None
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = None

    if cfg is not None:
        path_patterns = cfg.get("path_test_commands") or {}
        if path_patterns:
            effective_paths = paths if paths is not None else _detect_changed_paths(repo_root)
            cmd = _match_path_test_command(path_patterns, effective_paths, repo_root)
            if cmd:
                return cmd
        cmd = cfg.get("test_command") or ""
        if cmd:
            return cmd

    tusk_bin = os.path.join(script_dir, "tusk")
    if os.path.isfile(tusk_bin) and os.access(tusk_bin, os.X_OK):
        result = _run([tusk_bin, "test-detect"], cwd=repo_root)
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                if payload.get("command") and payload.get("confidence") != "none":
                    return payload["command"]
            except json.JSONDecodeError:
                pass
    return ""


def run_test(test_command: str, repo_root: str) -> int:
    """Run the configured test command in a shell and return its exit code.

    Captures stdout/stderr and re-emits both on *our* stderr so that *our*
    stdout stays reserved for the final JSON payload — programmatic callers
    must be able to run ``json.loads(result.stdout)`` directly rather than
    fishing the last line out of interleaved test output.
    """
    result = subprocess.run(
        test_command,
        cwd=repo_root,
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        sys.stderr.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def main(argv):
    parser = argparse.ArgumentParser(
        prog="tusk test-precheck",
        description=(
            "Run the project's test command against HEAD (stashing and "
            "restoring any local changes by name) and report whether the "
            "failure is pre-existing."
        ),
    )
    parser.add_argument(
        "--command",
        default="",
        help="Explicit test command (overrides config test_command / tusk test-detect).",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help=(
            "Repo-root-relative paths used to resolve path_test_commands. "
            "When omitted, precheck auto-detects changed + untracked paths from git."
        ),
    )
    args = parser.parse_args(argv[2:])

    repo_root = argv[0]
    config_path = argv[1]
    script_dir = os.path.dirname(os.path.abspath(__file__))

    test_command = resolve_test_command(
        explicit=args.command,
        config_path=config_path,
        repo_root=repo_root,
        script_dir=script_dir,
        paths=args.paths,
    )
    if not test_command:
        print(
            "Error: no test command available — pass --command, set "
            'config.test_command, or ensure `tusk test-detect` returns a command.',
            file=sys.stderr,
        )
        return 1

    try:
        dirty = detect_dirty(repo_root)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    stashed = False
    stash_message = ""
    if dirty:
        # Unique stash message — uuid + pid prevents collisions even when
        # multiple tusk invocations run in parallel.
        stash_message = f"tusk-test-precheck/{os.getpid()}/{uuid.uuid4().hex[:8]}"
        push_res = _run(
            ["git", "stash", "push", "--include-untracked", "-m", stash_message],
            cwd=repo_root,
        )
        if push_res.returncode != 0:
            print(
                f"Error: `git stash push` failed: {push_res.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        try:
            stash_ref = find_stash_ref_by_message(repo_root, stash_message)
        except RuntimeError as e:
            # push succeeded but we can't inspect the stash list — do NOT
            # run tests, because we also can't guarantee we'll be able to
            # pop our entry afterwards.  Surface the stash message so the
            # user can recover manually.
            print(
                f"Error: {e}\n"
                f"Stash entry '{stash_message}' was created but could not be "
                "verified.  Inspect `git stash list` and pop it manually.",
                file=sys.stderr,
            )
            return 1
        if not stash_ref:
            # Push reported success but our entry is not in the list.  This
            # is the exact silent-data-loss pattern TASK-55 exists to
            # prevent — treat it as a hard error, never a silent fall-through.
            print(
                f"Error: `git stash push` reported success but entry "
                f"'{stash_message}' is not in `git stash list`.  Your local "
                "changes may be in an inconsistent state.  Inspect `git "
                "stash list` and `git fsck --lost-found` before retrying.",
                file=sys.stderr,
            )
            return 1
        stashed = True

    exit_code = 1
    run_test_error: Exception | None = None
    try:
        exit_code = run_test(test_command, repo_root)
    except Exception as e:
        # run_test raising (FileNotFoundError, OSError, etc.) must still
        # trigger the stash-pop cleanup path — the alternative is leaving
        # the user's changes orphaned in the stash list.  Record the
        # exception and surface it after cleanup.
        run_test_error = e
    finally:
        if stashed:
            # Look up the ref again — another `git stash push` could have
            # bumped our entry off the top while the tests ran.
            try:
                stash_ref = find_stash_ref_by_message(repo_root, stash_message)
            except RuntimeError as e:
                print(
                    f"Error: {e}\n"
                    f"Your changes remain in the stash list under message "
                    f"'{stash_message}'.  Locate the entry with `git stash "
                    "list` and pop it manually.",
                    file=sys.stderr,
                )
                return 1
            if not stash_ref:
                print(
                    f"Error: stash entry '{stash_message}' disappeared while "
                    "tests were running — your changes may be lost.  "
                    "Inspect `git stash list` and `git fsck --lost-found` "
                    "to recover.",
                    file=sys.stderr,
                )
                return 1
            pop_res = _run(["git", "stash", "pop", stash_ref], cwd=repo_root)
            if pop_res.returncode != 0:
                print(
                    f"Error: `git stash pop {stash_ref}` failed — your changes "
                    f"remain in the stash list (message: {stash_message}).  "
                    f"Resolve conflicts and run `git stash pop {stash_ref}` "
                    f"manually.\n{pop_res.stderr}",
                    file=sys.stderr,
                )
                return 1

    if run_test_error is not None:
        print(
            f"Error: test command '{test_command}' raised: {run_test_error!r}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({
        "pre_existing": exit_code != 0,
        "exit_code": exit_code,
        "test_command": test_command,
        "stashed": stashed,
    }))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk test-precheck [--command <cmd>]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
