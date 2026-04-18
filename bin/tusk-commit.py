#!/usr/bin/env python3
"""Lint, stage, and commit in one atomic operation.

Called by the tusk wrapper (three equivalent forms):
    tusk commit <task_id> "<message>" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify] [--skip-lint] [--verbose]
    tusk commit <task_id> <file1> [file2 ...] -m "<message>" [--criteria <id>] ... [--skip-verify] [--skip-lint] [--verbose]
    tusk commit <task_id> <file1> [file2 ...] -- -m "<message>" [--criteria <id>] ... [--skip-verify] [--skip-lint] [--verbose]

The -m flag extracts the message; bare -- separators are silently ignored.
A [TASK-N] prefix in the message is stripped automatically to prevent duplication.

Arguments received from tusk:
    sys.argv[1] — repo root
    sys.argv[2] — config path
    sys.argv[3:] — task_id, message, files, and optional flags
                   (-m, --criteria, --skip-verify, --skip-lint, --verbose)

Steps:
    0. Validate file paths — fail fast before lint/tests if any path is missing or escapes repo root
    1. Run tusk lint --quiet — aborts on any non-advisory violation (exit 6).
       Advisory-only rules warn but never block. Bypass with --skip-lint or --skip-verify.
    2. Run test_command gate: use domain_test_commands[task.domain] if present, else test_command (hard-blocks on failure)
    3. Stage files: git add for all files (handles additions, modifications, and deletions)
    4. git commit with [TASK-<id>] <message> format and Co-Authored-By trailer
    5. For each criterion ID passed via --criteria, call tusk criteria done <id> (captures HEAD automatically)

Output contract (GitHub Issue #450):
    - test_command output is captured by default (not streamed) so background-task
      callers can read the final status without scrolling past 300KB of pytest output.
      Pass --verbose to stream test output live (useful for interactive debugging).
    - On test failure or timeout in quiet mode, the captured stdout/stderr is dumped
      before the error message so the failure is diagnosable.
    - On test success in quiet mode, a one-line "tests passed (<elapsed>s)" marker is emitted.
    - Lint output is run with --quiet: only rules with violations print. Passing rules
      are suppressed entirely. A one-line advisory summary prints when only advisory
      rules fired.
    - The last line of stdout is ALWAYS a single-line summary prefixed with
      "TUSK_COMMIT_RESULT: " followed by JSON: {status, exit_code, commit, task}.
      This line is findable via `tail -1` for every exit path.

Exit codes:
    0 — success
    1 — usage or validation error (bad arguments, invalid task ID, etc.)
    2 — test_command failed (nothing was staged or committed)
    3 — git add or git commit failed
    4 — one or more criteria could not be marked done (commit itself succeeded)
    5 — test_command exceeded its configured timeout (see test_command_timeout_sec)
    6 — tusk lint reported a non-advisory violation (nothing was staged or committed).
        Fix the violations, or bypass with --skip-lint / --skip-verify.
"""

import json
import os
import re
import subprocess
import sys
import time


TRAILER = "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"

# Prefix for the single-line final status summary emitted as the last line of
# stdout.  The summary is always the last line, so `tail -1` alone is enough
# for well-behaved captures; the tag prefix is for the messy case where a
# background harness interleaves stdout/stderr in one file and the line's
# position is no longer guaranteed — `grep TUSK_COMMIT_RESULT` recovers it.
# See GitHub Issue #450.
SUMMARY_PREFIX = "TUSK_COMMIT_RESULT:"


def _emit_final_summary(exit_code: int, state: dict) -> None:
    """Emit a single-line JSON summary as the last line of stdout.

    The summary is the only contract for background-task callers (e.g. Claude
    Code's truncated read-back) — it must be findable via `tail -1` regardless
    of what test or lint output came before it.
    """
    payload = {
        "status": "success" if exit_code == 0 else "failure",
        "exit_code": exit_code,
        "commit": state.get("sha"),
        "task": state.get("task_id"),
    }
    sys.stderr.flush()
    print(f"{SUMMARY_PREFIX} {json.dumps(payload, separators=(',', ':'))}", flush=True)


def _make_relative(abs_path: str, repo_root: str) -> str:
    """Return abs_path relative to repo_root.

    Both arguments should be symlink-resolved (os.path.realpath) so that
    symlink divergence between the user's CWD and the stored repo_root cannot
    produce '..' components.  On macOS (case-insensitive APFS/HFS+), abs_path
    and repo_root may share the same filesystem location but differ in case
    (e.g. /Users/foo/Desktop vs /Users/foo/desktop).  os.path.relpath is a
    byte-exact string comparison and would produce an incorrect
    '../../Desktop/...' path in that situation, which git add then rejects with
    a pathspec error (GitHub Issue #363).

    We detect this by comparing lower-cased forms of the paths.  If abs_path's
    lower-case form starts with repo_root's lower-case prefix, we strip the
    prefix directly rather than using relpath, preserving the user-supplied case
    in the file-specific suffix — which is what git add actually needs.
    """
    if sys.platform == "darwin":
        prefix = repo_root if repo_root.endswith(os.sep) else repo_root + os.sep
        if abs_path.lower().startswith(prefix.lower()):
            return abs_path[len(prefix):]
    return os.path.relpath(abs_path, repo_root)


def _escapes_root(real_abs: str, real_repo_root: str) -> bool:
    """Return True if real_abs is not inside real_repo_root.

    On macOS (case-insensitive APFS/HFS+), path components can differ in case
    (e.g. /Users/foo/desktop vs /Users/foo/Desktop) while pointing to the same
    inode.  os.path.realpath() does NOT canonicalize case on macOS — it only
    resolves symlinks — so a plain os.path.relpath comparison produces false
    positives when the stored repo root and the active CWD differ in case.
    We fold case on Darwin before the comparison to match the filesystem's rules.
    """
    if sys.platform == "darwin":
        rel = os.path.relpath(real_abs.lower(), real_repo_root.lower())
    else:
        rel = os.path.relpath(real_abs, real_repo_root)
    return rel.startswith("..")


def run(args: list[str], check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=check, cwd=cwd)


def _get_staged_deletions(repo_root: str) -> set[str]:
    """Return repo-root-relative paths currently staged as deletions.

    Uses ``git diff --cached --name-status -z`` so paths with embedded
    special characters survive the parse. Renames and copies carry two
    path tokens (old + new) and are skipped — neither is a pure deletion
    of the user-supplied path.

    Paths returned here must be excluded from ``git add`` in Step 3
    (TASK-67): the gitignore-retry branch force-adds with ``-f``, which
    would silently re-add the deleted file and defeat the deletion.
    """
    result = run(
        ["git", "diff", "--cached", "--name-status", "-z"],
        check=False, cwd=repo_root,
    )
    if result.returncode != 0 or not result.stdout:
        return set()
    deletions: set[str] = set()
    tokens = result.stdout.split("\0")
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        if status[:1] in ("R", "C"):
            i += 3
            continue
        if status.startswith("D") and i + 1 < len(tokens):
            deletions.add(tokens[i + 1])
        i += 2
    return deletions


def _print_error(msg: str) -> None:
    """Print an error to both stderr (interactive) and stdout (background-task output file capture)."""
    print(msg, file=sys.stderr)
    print(msg, flush=True)


def load_task_domain(tusk_bin: str, task_id: int) -> str:
    """Return the domain of the given task, or empty string if unavailable."""
    try:
        result = subprocess.run(
            [tusk_bin, "shell", f"SELECT COALESCE(domain, '') FROM tasks WHERE id = {task_id}"],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def load_test_command(config_path: str, domain: str = "") -> str:
    """Load the effective test command from config.

    Prefers domain_test_commands[domain] when the task has a domain and a
    matching entry exists.  Falls back to the global test_command otherwise.
    Returns an empty string when no command is configured.
    """
    try:
        with open(config_path) as f:
            config = json.load(f)
        if domain:
            cmd = config.get("domain_test_commands", {}).get(domain)
            if cmd:
                return cmd
        return config.get("test_command", "") or ""
    except Exception:
        return ""


DEFAULT_TEST_COMMAND_TIMEOUT_SEC = 120


def load_test_command_timeout(config_path: str) -> tuple[int, str]:
    """Return (timeout_seconds, source) for the test_command subprocess.

    Resolution order:
      1. TUSK_TEST_COMMAND_TIMEOUT env var (must parse as a positive int)
      2. config["test_command_timeout_sec"] (must parse as a positive int)
      3. DEFAULT_TEST_COMMAND_TIMEOUT_SEC (120)

    source is one of: "env", "config", "default".  Invalid values at any layer
    fall through to the next layer — the timeout is advisory infrastructure,
    not worth aborting the commit over a bad config value.
    """
    env_val = os.environ.get("TUSK_TEST_COMMAND_TIMEOUT")
    if env_val is not None:
        try:
            n = int(env_val)
            if n > 0:
                return n, "env"
        except ValueError:
            pass
    try:
        with open(config_path) as f:
            config = json.load(f)
        cfg_val = config.get("test_command_timeout_sec")
        if cfg_val is not None:
            n = int(cfg_val)
            if n > 0:
                return n, "config"
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return DEFAULT_TEST_COMMAND_TIMEOUT_SEC, "default"


def main(argv: list[str]) -> int:
    """Entry point — wraps _run_commit so a final summary line is always emitted.

    The summary (see _emit_final_summary) is the single contract for
    background-task callers that truncate stdout; it must be the last line of
    stdout for every exit path, including argument-validation failures.
    """
    state: dict = {"sha": None, "task_id": None}
    exit_code = 1
    try:
        exit_code = _run_commit(argv, state)
        return exit_code
    finally:
        _emit_final_summary(exit_code, state)


def _run_commit(argv: list[str], state: dict) -> int:
    if len(argv) < 4:
        print(
            "Usage: tusk commit <task_id> \"<message>\" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify] [--verbose]",
            file=sys.stderr,
        )
        return 1

    repo_root = argv[0]
    config_path = argv[1]
    remaining = argv[2:]

    # Parse flags out of remaining args; collect everything else positionally.
    # Recognised flags: --criteria <id>..., --skip-verify, -m <msg>
    # The bare "--" token is silently dropped (AI callers sometimes insert it as
    # a separator between files and message).
    criteria_ids: list[str] = []
    skip_verify: bool = False
    skip_lint: bool = False
    verbose: bool = False
    flag_message: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(remaining):
        if remaining[i] == "--criteria":
            i += 1
            collected = 0
            while i < len(remaining) and not remaining[i].startswith("--") and remaining[i] != "-m":
                criteria_ids.append(remaining[i])
                i += 1
                collected += 1
            if collected == 0:
                print("Error: --criteria requires at least one argument", file=sys.stderr)
                return 1
        elif remaining[i] == "--skip-verify":
            skip_verify = True
            i += 1
        elif remaining[i] == "--skip-lint":
            skip_lint = True
            i += 1
        elif remaining[i] == "--verbose":
            verbose = True
            i += 1
        elif remaining[i] == "-m":
            i += 1
            if i >= len(remaining):
                print("Error: -m requires a message argument", file=sys.stderr)
                return 1
            flag_message = remaining[i]
            i += 1
        elif remaining[i] == "--":
            # Silently ignore bare -- separators
            i += 1
        else:
            positional.append(remaining[i])
            i += 1

    # Determine task_id, message, and files from the positional args.
    # Two invocation forms are supported:
    #   1. Positional:  <task_id> "<message>" <files...>       (original form)
    #   2. Flag:        <task_id> <files...> -m "<message>"    (git-like form)
    if flag_message is not None:
        # -m was used: positional = [task_id, files...]
        if len(positional) < 2:
            print(
                "Usage: tusk commit <task_id> <file1> [file2 ...] -m \"<message>\" [--criteria <id>] ... [--skip-verify]",
                file=sys.stderr,
            )
            return 1
        task_id_str = positional[0]
        message = flag_message
        files = positional[1:]
    else:
        # Original positional form: task_id message files...
        if len(positional) < 3:
            print(
                "Usage: tusk commit <task_id> \"<message>\" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify]",
                file=sys.stderr,
            )
            return 1
        task_id_str = positional[0]
        message = positional[1]
        files = positional[2:]

    # Validate task_id is an integer
    try:
        task_id = int(task_id_str)
    except ValueError:
        print(f"Error: Invalid task ID: {task_id_str}", file=sys.stderr)
        return 1
    state["task_id"] = task_id

    # Validate criteria IDs are integers
    for cid in criteria_ids:
        try:
            int(cid)
        except ValueError:
            print(f"Error: Invalid criterion ID: {cid}", file=sys.stderr)
            return 1

    # Strip duplicate [TASK-N] prefix — AI callers sometimes include it in the
    # message even though tusk commit prepends it automatically.
    message = re.sub(r"^\[TASK-\d+\]\s*", "", message)

    if not message.strip():
        print("Error: Commit message must not be empty", file=sys.stderr)
        return 1

    # ── Announce status lines? ───────────────────────────────────────
    # Status banners ("starting TASK-N", "=== Running tusk lint ===",
    # "=== Staging ===", "=== Creating commit ===", "=== Marking criterion ===")
    # are noise for skill callers (non-TTY stderr) that only parse the final
    # TUSK_COMMIT_RESULT line. Gate them on --verbose or an interactive stderr.
    announce_status = verbose or sys.stderr.isatty()

    # ── Startup sentinel ─────────────────────────────────────────────
    # Written to stdout immediately so that background-task output-file
    # capture has a non-empty file even when the process exits early.
    if announce_status:
        print(f"tusk commit: starting TASK-{task_id}", flush=True)

    # ── Step → exit-code map (quick reference for diagnosis) ─────────
    #   Step 0  (path validation)   → exit 3  (escapes root or path not found)
    #   Step 1  (lint)              → exit 6  (non-advisory lint violation;
    #                                          bypass with --skip-lint / --skip-verify)
    #   Step 2  (test_command gate) → exit 2  (test_command failed)
    #   Step 3  (git add)           → exit 3  (git add failed)
    #   Step 4  (git commit)        → exit 3  (git commit failed)
    #   Step 5  (criteria done)     → exit 4  (one or more criteria failed)
    #   Argument / validation errors before Step 0 → exit 1

    # ── Step 0: Validate file paths (fail fast before lint/tests) ────
    # Resolve relative paths against the caller's CWD before making them relative to
    # repo_root.  This lets users in a monorepo subdirectory pass paths that are relative
    # to their working directory (e.g. `tests/foo.py` from inside `apps/scraper/`) rather
    # than requiring repo-root-relative paths.  Absolute paths are passed through unchanged.
    caller_cwd = os.getcwd()
    # Canonicalize repo_root via realpath so that the escape check works correctly on
    # case-insensitive filesystems (e.g. macOS) where git may return a lowercase root
    # path while the actual CWD uses the filesystem-canonical capitalisation.
    real_repo_root = os.path.realpath(repo_root)
    resolved_files: list[str] = []
    escape_errors: list[tuple[str, str]] = []
    for f in files:
        if os.path.isabs(f):
            abs_path = os.path.normpath(f)
            real_abs = os.path.realpath(abs_path)
            if _escapes_root(real_abs, real_repo_root):
                escape_errors.append((f, abs_path))
            resolved_files.append(abs_path)
        else:
            abs_path_cwd = os.path.normpath(os.path.join(caller_cwd, f))
            abs_path_root = os.path.normpath(os.path.join(repo_root, f))
            # Prefer CWD-relative if it exists (original monorepo use case).
            # Fall back to repo-root-relative when the CWD-relative path is
            # missing — this prevents the doubled-prefix failure that occurs
            # when caller_cwd is a subdirectory whose name is also the first
            # component of the file path (e.g., CWD=repo/svc/, path=svc/foo.py).
            if os.path.exists(abs_path_cwd):
                abs_path = abs_path_cwd
            elif os.path.exists(abs_path_root):
                abs_path = abs_path_root
            else:
                abs_path = abs_path_cwd  # let pre-flight emit the diagnostic
            # realpath is used only for the escape check: resolving symlinks
            # and case differences ensures _escapes_root gives the correct
            # answer on all platforms.  It must NOT be used to compute the
            # path we hand to git add — if a directory component is a symlink
            # (e.g. apps/web -> packages/web), realpath would silently replace
            # the symlink name with its target, producing a path git doesn't
            # recognise (GitHub Issue #365).
            #
            # We pass real_repo_root (not repo_root) to _make_relative so that a
            # symlinked repo root (e.g. sym_repo -> real_repo, GitHub Issue #628)
            # is resolved before the prefix comparison — without this, the relpath
            # fallback inside _make_relative produces '..' components.  Critically,
            # abs_path is NOT realpath'd, preserving symlink names inside the file
            # path.  _make_relative's case-insensitive prefix logic handles the
            # macOS case-divergence scenario (#363) without requiring realpath on
            # abs_path.
            real_abs = os.path.realpath(abs_path)
            if _escapes_root(real_abs, real_repo_root):
                escape_errors.append((f, abs_path))
            resolved = _make_relative(abs_path, real_repo_root)
            resolved_files.append(resolved)

    if escape_errors:
        for orig, abs_path in escape_errors:
            _print_error(
                f"Error: path escapes the repo root: '{orig}'\n"
                f"  Resolved to: '{abs_path}'\n"
                f"  Repo root is: {repo_root}\n"
                f"  Hint: paths must be inside the repo root"
            )
        return 3

    # Belt-and-suspenders: reject any resolved path that still contains '..' components.
    # _make_relative() should never produce such paths, but if a future code path does,
    # os.path.exists() would silently resolve through '..' and the error would surface
    # later as a confusing 'git add failed' message.
    dotdot_errors = [
        (orig, resolved)
        for orig, resolved in zip(files, resolved_files)
        if not os.path.isabs(resolved)
        and ".." in resolved.replace(os.sep, "/").split("/")
    ]
    if dotdot_errors:
        for orig, resolved in dotdot_errors:
            _print_error(
                f"Error: resolved path contains '..' components: '{orig}'\n"
                f"  Resolved to: '{resolved}'\n"
                f"  Hint: paths must not traverse outside the repo root"
            )
        return 3

    # Pre-flight: verify each resolved path exists so we can emit a useful diagnostic
    # before git produces a cryptic "pathspec did not match" error.
    # Exception: files absent from disk but still tracked by git are valid deletions —
    # `git add` stages their removal natively and they must not be rejected as missing.
    not_on_disk = [
        (orig, resolved)
        for orig, resolved in zip(files, resolved_files)
        if not os.path.exists(resolved if os.path.isabs(resolved) else os.path.join(repo_root, resolved))
    ]
    missing = not_on_disk
    if not_on_disk:
        # Convert to repo-root-relative paths for `git ls-files` (which outputs relative paths).
        rel_for_git = [
            os.path.relpath(resolved, repo_root) if os.path.isabs(resolved) else resolved
            for _, resolved in not_on_disk
        ]
        ls = run(
            ["git", "ls-files", "--"] + rel_for_git,
            check=False,
            cwd=repo_root,
        )
        git_tracked = set(ls.stdout.splitlines())
        # Files already staged as deletions (via `git rm`) are legitimate —
        # they are absent from disk AND from `git ls-files` (the rm removed
        # them from the index) but appear in `git diff --cached` as 'D'.
        # Treat them as valid inputs so Step 3 can commit the staged deletion.
        staged_deletions = _get_staged_deletions(repo_root)
        missing = [
            (orig, resolved)
            for (orig, resolved), rel in zip(not_on_disk, rel_for_git)
            if rel not in git_tracked and rel not in staged_deletions
        ]
    if missing:
        for orig, resolved in missing:
            was_remapped = orig != resolved
            glob_hint = (
                "\n  Hint: path contains shell glob characters ([, ], *, ?)."
                " In zsh these are expanded by the shell before tusk receives them."
                " Wrap the path in double quotes when calling tusk commit:"
                f' tusk commit ... "{orig}" ...'
                if any(c in orig for c in "[]?*")
                else ""
            )
            if not was_remapped:
                _print_error(
                    f"Error: path not found: '{orig}'\n"
                    f"  Hint: paths must exist relative to the repo root ({repo_root})"
                    f"{glob_hint}"
                )
            else:
                _print_error(
                    f"Error: path not found: '{orig}'\n"
                    f"  Resolved to (repo-root-relative): '{resolved}'\n"
                    f"  Hint: the file was not found at {os.path.join(repo_root, resolved)}"
                    f"{glob_hint}"
                )
        return 3

    # ── Step 1: Run lint (blocks on non-advisory violations) ─────────
    # `tusk lint --quiet` prints ONLY rules with violations — passing rules
    # are suppressed so a clean repo produces no lint output at all during
    # commit.  Non-advisory violations exit 1; we translate that to exit 6
    # to give the aborted-by-lint case its own distinct code, separate from
    # tests (2), git (3), criteria (4), and timeout (5).
    # Advisory-only warnings (Rules 13, 14, 15, 17, 20, 22, 23) print their
    # findings but leave lint's exit status at 0, so they never block here.
    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")
    if skip_verify or skip_lint:
        if announce_status:
            reason = "--skip-lint" if skip_lint else "--skip-verify"
            print(f"=== Skipping tusk lint ({reason}) ===")
            sys.stdout.flush()
    else:
        if announce_status:
            print("=== Running tusk lint ===")
            sys.stdout.flush()
        lint = subprocess.run([tusk_bin, "lint", "--quiet"], capture_output=False)
        if lint.returncode != 0:
            _print_error(
                "\nError: tusk lint reported non-advisory violations — aborting commit.\n"
                "  Fix the violations above, or bypass with --skip-lint "
                "(lint only) or --skip-verify (lint, tests, and pre-commit hooks)."
            )
            return 6
        if announce_status:
            print()
        sys.stdout.flush()

    # ── Step 2: Run test_command gate (hard-blocks on failure) ───────
    # Only query the task's domain when domain_test_commands is configured —
    # avoids a DB round-trip for the common case where domain routing is unused.
    task_domain = ""
    try:
        with open(config_path) as _f:
            _cfg = json.load(_f)
        if _cfg.get("domain_test_commands"):
            task_domain = load_task_domain(tusk_bin, task_id)
    except Exception:
        pass
    test_cmd = load_test_command(config_path, task_domain)
    if test_cmd and not skip_verify:
        timeout_sec, timeout_source = load_test_command_timeout(config_path)
        # Only announce the command up-front in verbose mode.  In quiet mode
        # (the default) we keep stdout short so background-task callers can find
        # the final summary line with `tail -1` instead of scrolling through
        # 300KB of pytest output.
        if verbose:
            print(f"=== Running test_command: {test_cmd} (timeout {timeout_sec}s) ===")
            sys.stdout.flush()
        started = time.monotonic()
        try:
            test = subprocess.run(
                test_cmd,
                shell=True,
                capture_output=not verbose,
                text=True,
                cwd=repo_root,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            # When capture_output=True, TimeoutExpired carries whatever was
            # collected on stdout/stderr before the child was killed — dump it
            # first so the user can see which test hung.  text=True above means
            # exc.stdout/exc.stderr are already str (or None).
            if not verbose:
                if exc.stdout:
                    sys.stdout.write(exc.stdout)
                    sys.stdout.flush()
                if exc.stderr:
                    sys.stderr.write(exc.stderr)
                    sys.stderr.flush()
            source_hint = {
                "env": "TUSK_TEST_COMMAND_TIMEOUT env var",
                "config": 'config key "test_command_timeout_sec"',
                "default": 'default (override with "test_command_timeout_sec" in tusk/config.json '
                           'or TUSK_TEST_COMMAND_TIMEOUT env var)',
            }[timeout_source]
            _print_error(
                f"\nError: test_command timed out after {timeout_sec}s — aborting commit\n"
                f"  Command: {test_cmd}\n"
                f"  Timeout source: {source_hint}\n"
                f"  Hint: if the command needs more time, raise the limit; "
                f"if it hangs waiting for input (e.g. interactive mode), switch to a non-interactive form."
            )
            return 5
        elapsed = time.monotonic() - started
        if test.returncode != 0:
            # Dump the captured output so the failure is diagnosable even in
            # quiet mode.  In verbose mode the output already streamed live, so
            # there is nothing to dump.
            if not verbose:
                if test.stdout:
                    sys.stdout.write(test.stdout)
                    sys.stdout.flush()
                if test.stderr:
                    sys.stderr.write(test.stderr)
                    sys.stderr.flush()
            print(
                f"\nError: test_command failed (exit {test.returncode}, {elapsed:.1f}s) — aborting commit",
                file=sys.stderr,
            )
            return 2
        print(f"tests passed ({elapsed:.1f}s)")
        sys.stdout.flush()

    # ── Step 2.5: Stage unstaged deletions of tracked files ─────────
    # GitHub Issue #474: when tracked files are removed via `rm`/`rm -rf`
    # rather than `git rm`, they remain in the index with a "deleted from
    # working tree" marker until the next `git add` sees them.  Scan for
    # these now and append to resolved_files so the Step 3 git add call
    # captures both the explicit paths and the implicit deletions in a
    # single commit — otherwise the deletions surface as unstaged changes
    # after commit and require a manual `git rm && git commit` follow-up.
    deleted = run(["git", "ls-files", "--deleted", "-z"], check=False, cwd=repo_root)
    if deleted.returncode == 0 and deleted.stdout:
        # resolved_files holds either absolute paths or repo-root-relative
        # paths; git ls-files emits repo-root-relative paths.  Normalize
        # before deduping so a user-supplied deleted path (which TASK-679
        # allows through pre-flight) is not staged twice.
        already_listed = {
            os.path.relpath(f, repo_root) if os.path.isabs(f) else f
            for f in resolved_files
        }
        extra_deletions = [
            d for d in deleted.stdout.split("\0") if d and d not in already_listed
        ]
        if extra_deletions:
            print(
                f"Note: auto-staging {len(extra_deletions)} unstaged "
                "deletion(s) of tracked file(s) (from rm/rm -rf):"
            )
            for d in extra_deletions:
                print(f"  - {d}")
            resolved_files = resolved_files + extra_deletions

    # ── Step 3: Stage files ──────────────────────────────────────────
    # File paths were already resolved and validated in Step 0.
    # git add handles deletions of tracked files natively since Git 2.x — no git rm needed.
    # The -- separator prevents git from misinterpreting file paths as options.
    #
    # Paths already staged as deletions (e.g. via `git rm`) MUST NOT be passed
    # to `git add` (TASK-67): the gitignore-retry branch force-adds with `-f`
    # and would silently re-add the deleted file to the index, defeating the
    # deletion. Partition them out; they ride along into the commit through
    # their existing staged state.
    staged_deletion_set = _get_staged_deletions(repo_root)
    rel_for_diff = [
        os.path.relpath(f, repo_root) if os.path.isabs(f) else f
        for f in resolved_files
    ]
    to_add = [
        f for f, rel in zip(resolved_files, rel_for_diff)
        if rel not in staged_deletion_set
    ]
    skipped_deletions = len(resolved_files) - len(to_add)

    if announce_status:
        if to_add and skipped_deletions:
            print(
                f"=== Staging {len(to_add)} file(s) "
                f"(plus {skipped_deletions} already-staged deletion(s)) ==="
            )
        elif to_add:
            print(f"=== Staging {len(to_add)} file(s) ===")
        else:
            print(f"=== Committing {skipped_deletions} already-staged deletion(s) ===")
        sys.stdout.flush()

    if to_add:
        result = run(["git", "add", "--"] + to_add, check=False, cwd=repo_root)
    else:
        # Deletion-only commit: nothing to add; the index already holds the
        # staged deletions. Fabricate a success result so the existing flow
        # falls straight through to Step 4.
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    if result.returncode != 0:
        stderr_text = result.stderr.strip()

        # Special case: a hook (e.g. lint-staged) may have already staged these files,
        # leaving the working tree clean so git add finds nothing to update and exits
        # non-zero with "pathspec did not match any files".  If every requested file is
        # already present in the index, treat the add as a no-op and proceed to commit.
        if "pathspec" in stderr_text and "did not match" in stderr_text:
            rel_resolved = [
                os.path.relpath(f, repo_root) if os.path.isabs(f) else f
                for f in to_add
            ]
            cached = run(
                ["git", "ls-files", "--cached", "--"] + rel_resolved,
                check=False,
                cwd=repo_root,
            )
            cached_set = set(cached.stdout.splitlines())
            if all(f in cached_set for f in rel_resolved):
                print(
                    "Note: all files are already staged in the index "
                    "(a hook such as lint-staged may have pre-staged them) — "
                    "proceeding to commit."
                )
                # Fall through to Step 4 — no return here.
                stderr_text = None  # suppress the error block below

        if stderr_text is not None:
            files_str = " ".join(to_add)
            # Classify each file: tracked status (authoritative) + gitignore
            # rule (diagnostic).  `git ls-files --error-unmatch` exits 0 iff
            # the path is already tracked.  `git check-ignore --no-index -v`
            # reports the matching rule even for tracked paths — plain
            # `check-ignore` skips tracked files, which is why a tracked file
            # under a gitignored directory (e.g. tusk/config.json with /tusk/
            # in .gitignore) would previously fall through the retry logic
            # without the --no-index flag (TASK-88).
            per_file = []  # (path, is_tracked, rule_or_None)
            for f in to_add:
                ls = run(
                    ["git", "ls-files", "--error-unmatch", "--", f],
                    check=False, cwd=repo_root,
                )
                is_tracked = ls.returncode == 0
                ci = run(
                    ["git", "check-ignore", "--no-index", "-v", "--", f],
                    check=False, cwd=repo_root,
                )
                rule = (
                    ci.stdout.strip()
                    if ci.returncode == 0 and ci.stdout.strip()
                    else None
                )
                per_file.append((f, is_tracked, rule))

            tracked_ignored = [(f, rule) for f, t, rule in per_file if t and rule]
            untracked_ignored = [(f, rule) for f, t, rule in per_file if not t and rule]

            if untracked_ignored:
                # Refuse to force-add untracked gitignored files — doing so can
                # silently pull in build artifacts, .env files, or other content
                # the .gitignore exists to exclude. The user must opt in manually
                # via `git add -f` if they really want to track the file.
                _print_error(
                    f"Error: git add failed (cwd: {repo_root}):\n"
                    f"  Command: git add -- {files_str}\n"
                    f"  {stderr_text}"
                )
                for f, rule in untracked_ignored:
                    _print_error(
                        f"  Refusing to force-add untracked gitignored file:\n"
                        f"    {f}\n"
                        f"    Rule: {rule}\n"
                        f"  Hint: if you really want to track it, run `git add -f {f}` "
                        f"manually, then retry `tusk commit`."
                    )
            elif tracked_ignored:
                # All blocked paths are already tracked — safe to force-add.
                # Covers the /tusk/ gitignored + tusk/config.json tracked pattern
                # and the .claude/skills/<skill>/SKILL.md after-first-commit pattern.
                tracked_paths = [f for f, _ in tracked_ignored]
                tracked_set = {f for f, _ in tracked_ignored}
                non_blocked = [f for f in to_add if f not in tracked_set]
                print(
                    f"Note: {len(tracked_paths)} tracked file(s) blocked by "
                    ".gitignore — retrying with `git add -f` "
                    "(tracked files are safe to force-add)."
                )
                retry_ok = True
                r_force = run(
                    ["git", "add", "-f", "--"] + tracked_paths,
                    check=False, cwd=repo_root,
                )
                if r_force.returncode != 0:
                    retry_ok = False
                    _print_error(
                        f"Error: git add -f also failed:\n  {r_force.stderr.strip()}"
                    )
                if retry_ok and non_blocked:
                    r_rest = run(
                        ["git", "add", "--"] + non_blocked,
                        check=False, cwd=repo_root,
                    )
                    if r_rest.returncode != 0:
                        retry_ok = False
                        _print_error(
                            f"Error: git add failed for non-ignored files:\n"
                            f"  {r_rest.stderr.strip()}"
                        )
                if retry_ok:
                    stderr_text = None  # all files staged — fall through to commit
                else:
                    _print_error(
                        f"Error: git add failed (cwd: {repo_root}):\n"
                        f"  Command: git add -- {files_str}\n"
                        f"  {stderr_text}"
                    )
                    for f, rule in tracked_ignored:
                        _print_error(
                            f"  Gitignore rule blocking '{f}':\n"
                            f"    {rule}\n"
                            f"  Hint: use `git add -f {f}` to force-add, then commit manually."
                        )
            else:
                _print_error(
                    f"Error: git add failed (cwd: {repo_root}):\n"
                    f"  Command: git add -- {files_str}\n"
                    f"  {stderr_text}"
                )
                if "ignored by" in stderr_text or ".gitignore" in stderr_text:
                    # git reported gitignore but neither ls-files nor check-ignore
                    # could attribute it to a specific path.  Leave the user with
                    # the manual workaround.
                    _print_error(
                        "  Hint: one or more files are excluded by .gitignore — "
                        "use `git add -f <file>` to force-add, then commit manually."
                    )
                elif "sparse-checkout" in stderr_text:
                    _print_error(
                        "  Hint: one or more files are outside the git sparse-checkout cone — "
                        "run `git sparse-checkout add <directory>` to include them."
                    )

        if stderr_text is not None:
            return 3

    # ── Step 4: Commit ───────────────────────────────────────────────
    if announce_status:
        print("=== Creating commit ===")
        sys.stdout.flush()
    full_message = f"[TASK-{task_id}] {message}\n\n{TRAILER}"
    # Capture HEAD before committing so we can verify whether the commit
    # landed even when a hook (e.g. husky + lint-staged) exits non-zero.
    pre = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo_root)
    pre_sha = pre.stdout.strip() if pre.returncode == 0 else None

    commit_cmd = ["git", "commit", "-m", full_message]
    if skip_verify:
        commit_cmd.append("--no-verify")
    result = run(commit_cmd, check=False, cwd=repo_root)

    if result.returncode != 0:
        # Check whether the commit actually landed despite the non-zero exit.
        post = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo_root)
        post_sha = post.stdout.strip() if post.returncode == 0 else None
        commit_landed = post_sha and post_sha != pre_sha

        # Issue #477: an auto-formatter pre-commit hook (black, ruff --fix,
        # prettier, gofmt) may have rewritten tracked files in-place, leaving
        # the working tree ahead of the index so `git commit` aborted with
        # nothing new staged. Detect this by diffing the index against the
        # working tree for the files we staged; if any diverged, re-stage the
        # reformatted content and retry the commit exactly once.
        if not commit_landed and not skip_verify and to_add:
            diff_result = run(
                ["git", "diff", "--name-only", "--"] + to_add,
                check=False,
                cwd=repo_root,
            )
            reformatted = (
                [f for f in diff_result.stdout.splitlines() if f.strip()]
                if diff_result.returncode == 0
                else []
            )
            if reformatted:
                print(
                    f"Note: {len(reformatted)} file(s) modified by pre-commit hook "
                    "after staging — re-staging reformatted content and retrying commit once."
                )
                readd = run(
                    ["git", "add", "--"] + to_add, check=False, cwd=repo_root
                )
                if readd.returncode == 0:
                    result = run(commit_cmd, check=False, cwd=repo_root)
                    post = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo_root)
                    post_sha = post.stdout.strip() if post.returncode == 0 else None
                    commit_landed = post_sha and post_sha != pre_sha

        if not commit_landed:
            error_text = result.stderr.strip()
            _print_error(f"Error: git commit failed:\n{error_text}")
            hook_keywords = ("lint-staged", "pre-commit", "husky", "hook")
            if any(kw in error_text.lower() for kw in hook_keywords):
                _print_error(
                    "  Hint: a pre-commit hook rejected the commit. "
                    "An auto-formatter hook may have rewritten the file — "
                    "re-stage the reformatted content and retry, "
                    "or run with --skip-verify to bypass hooks: "
                    "tusk commit ... --skip-verify"
                )
            else:
                _print_error(
                    "  Hint: if a pre-commit hook is causing this, "
                    "try: tusk commit ... --skip-verify"
                )
            return 3

        # Commit landed but the last attempt emitted a non-zero exit (e.g.
        # lint-staged "no staged files" warning). Surface it as a note, not
        # a fatal error.
        if result.returncode != 0:
            warning = result.stderr.strip()
            if warning:
                print(f"Note: git hook warning (commit landed successfully):\n{warning}")

    if result.stdout.strip():
        print(result.stdout.strip())

    # Capture the landed commit SHA for the final summary line.  We re-query
    # rather than reusing post_sha from the rescue path because the common
    # fast-path (commit succeeds on the first try) never sets post_sha.
    head = run(["git", "rev-parse", "--short=12", "HEAD"], check=False, cwd=repo_root)
    if head.returncode == 0 and head.stdout.strip():
        state["sha"] = head.stdout.strip()

    # ── Step 5: Mark criteria done (captures new HEAD automatically) ─
    # When multiple criteria are batched in one commit call, suppress the
    # shared-commit warning for criteria[1:] — the user intentionally grouped them.
    criteria_failed = False
    for idx, cid in enumerate(criteria_ids):
        if announce_status:
            print(f"\n=== Marking criterion {cid} done ===")
            sys.stdout.flush()
        cmd = [tusk_bin, "criteria", "done", cid]
        if skip_verify:
            cmd.append("--skip-verify")
        if idx > 0 and len(criteria_ids) > 1:
            cmd.append("--batch")
        result = subprocess.run(cmd, capture_output=False, check=False)
        if result.returncode != 0:
            print(
                f"Warning: Failed to mark criterion {cid} done",
                file=sys.stderr,
            )
            criteria_failed = True

    if criteria_failed:
        print(
            "\nWarning: One or more criteria could not be marked done — "
            "check the output above and mark them manually with: tusk criteria done <id>",
            file=sys.stderr,
        )
        return 4

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not os.path.isdir(sys.argv[1]):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk commit <task_id> \"<message>\" <file1> [file2 ...] or: tusk commit <task_id> <file1> [file2 ...] -m \"<message>\"", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
