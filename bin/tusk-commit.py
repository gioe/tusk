#!/usr/bin/env python3
"""Test, stage, and commit in one atomic operation.

Called by the tusk wrapper (three equivalent forms):
    tusk commit <task_id> "<message>" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify] [--skip-lint] [--allow-branch-mismatch] [--verbose]
    tusk commit <task_id> <file1> [file2 ...] -m "<message>" [--criteria <id>] ... [--skip-verify] [--skip-lint] [--allow-branch-mismatch] [--verbose]
    tusk commit <task_id> <file1> [file2 ...] -- -m "<message>" [--criteria <id>] ... [--skip-verify] [--skip-lint] [--allow-branch-mismatch] [--verbose]

The -m flag extracts the message; bare -- separators are silently ignored.
A [TASK-N] prefix in the message is stripped automatically to prevent duplication.

Arguments received from tusk:
    sys.argv[1] — repo root
    sys.argv[2] — config path
    sys.argv[3:] — task_id, message, files, and optional flags
                   (-m, --criteria, --skip-verify, --skip-lint, --verbose)

Steps:
    0a. Validate current branch — refuses (exit 7) when the current branch does not match
        the task's recorded workspace branch, or when no workspace is recorded and HEAD
        is the default branch (issue #794). Bypass with --allow-branch-mismatch.
    0. Validate file paths — fail fast before lint/tests if any path is missing or escapes repo root
    1. Preflight git index lock creation, then run test_command gate:
       use path_test_commands when one pattern covers every staged path, else
       domain_test_commands[task.domain], else test_command (hard-blocks on failure).
       When path_test_commands_skip_unmatched is true and no staged path touches
       any configured path-test surface, info-skip the gate. Also info-skipped
       when every staged file is non-code — docs/markdown, GitHub workflow YAML,
       or scope.always_allowed metadata — since such commits cannot change test
       outcomes (issue #950).
    2. Stage files: git add for all files (handles additions, modifications, and deletions)
    3. git commit with [TASK-<id>] <message> format and Co-Authored-By trailer
    4. For each criterion ID passed via --criteria, call tusk criteria done <id> (captures HEAD automatically)

Output contract (GitHub Issue #450):
    - test_command output is captured by default (not streamed) so background-task
      callers can read the final status without scrolling past 300KB of pytest output.
      Pass --verbose to stream test output live (useful for interactive debugging).
    - On test failure or timeout in quiet mode, the captured stdout/stderr is dumped
      before the error message so the failure is diagnosable.
    - On test success in quiet mode, a one-line "tests passed (<elapsed>s)" marker is emitted.
    - Lint is enforced by `tusk merge`, not by `tusk commit`; --skip-lint is
      accepted for compatibility and has no effect.
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
    6 — reserved for the former commit-time lint gate.
    7 — current branch does not match the task's recorded workspace branch, or no workspace
        is recorded and HEAD is the default branch. Bypass with --allow-branch-mismatch.
    8 — reserved for the former commit-time lint timeout gate.
"""

import fnmatch
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py, tusk-worktree-command.py, tusk-git-helpers.py and tusk-db-lib.py

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
_worktree_command = tusk_loader.load("tusk-worktree-command")
_git_helpers = tusk_loader.load("tusk-git-helpers")
_db_lib = tusk_loader.load("tusk-db-lib")
open_sqlite = _db_lib.open_sqlite


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
    # Force pretty=False — this marker line must stay single-line regardless of
    # TUSK_PRETTY so callers can recover it via `tail -1` / `grep TUSK_COMMIT_RESULT`.
    print(f"{SUMMARY_PREFIX} {dumps(payload, pretty=False)}", flush=True)


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


def _git_index_lock_path(repo_root: str) -> str:
    """Return the actual index.lock path for normal and linked worktrees.

    Avoid a git subprocess here because many commit-path unit tests mock
    `git rev-parse` broadly for HEAD checks. The `.git` directory/file format
    is enough for the lock path we need to preflight.
    """
    git_entry = os.path.join(repo_root, ".git")
    if os.path.isdir(git_entry):
        return os.path.join(git_entry, "index.lock")
    if os.path.isfile(git_entry):
        try:
            content = open(git_entry, encoding="utf-8").read().strip()
        except OSError:
            return ""
        prefix = "gitdir:"
        if content.lower().startswith(prefix):
            git_dir = content[len(prefix):].strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.normpath(os.path.join(repo_root, git_dir))
            return os.path.join(git_dir, "index.lock")
    return ""


def _preflight_git_index_writable(repo_root: str) -> tuple[bool, str]:
    """Verify that git can create the index lock before expensive gates run."""
    lock_path = _git_index_lock_path(repo_root)
    if not lock_path:
        return True, ""
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        return (
            False,
            "Error: git index lock is already present — aborting before test_command.\n"
            f"  Lock path: {lock_path}\n"
            "  Hint: another git process may be running, or a stale index.lock "
            "may need to be removed after verifying no git process is active.",
        )
    except OSError as exc:
        return (
            False,
            "Error: git index is not writable — aborting before test_command.\n"
            f"  Lock path: {lock_path}\n"
            f"  {exc.strerror or exc}",
        )
    else:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except OSError as exc:
            return (
                False,
                "Error: git index lock preflight could not clean up — aborting before test_command.\n"
                f"  Lock path: {lock_path}\n"
                f"  {exc.strerror or exc}",
            )
    return True, ""


def _get_staged_deletions(repo_root: str) -> set[str]:
    """Return repo-root-relative paths absent from disk but staged for the next commit.

    Uses ``git diff --cached --name-status -z`` so paths with embedded
    special characters survive the parse. Includes:

    * ``D`` entries — pure staged deletions (e.g. ``git rm`` or ``rm`` + auto-stage).
    * ``R`` source paths — after ``git mv old new``, ``old`` is absent from
      the working tree and its deletion is staged at the index level. Callers
      treat the source the same as a ``D`` entry: it is a legitimate input that
      doesn't exist on disk, and it must not be passed to ``git add`` (Issue #554).

    Excludes:

    * ``C`` source paths — for a copy, the source remains in the working tree
      and the index, so it is neither absent from disk nor staged for removal.

    Paths returned here must be excluded from ``git add`` in Step 3
    (TASK-67): the gitignore-retry branch force-adds with ``-f``, which
    would silently re-add the deleted file and defeat the deletion. For
    ``R`` sources the same exclusion is required for a different reason:
    ``git add <absent-path>`` exits non-zero with ``pathspec did not match``.
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
        if status[:1] == "R":
            if i + 1 < len(tokens):
                deletions.add(tokens[i + 1])
            i += 3
            continue
        if status[:1] == "C":
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
    """Return the domain of the given task, or empty string if unavailable.

    Uses ``tusk -json "<SQL>"`` rather than ``tusk shell <SQL>`` — the latter
    has exited 1 since TASK-287 forbade positional SQL args to ``tusk
    shell``, which silently zeroed out domain detection here and meant
    ``load_test_command`` always fell through to the global ``test_command``
    (issue #836).  ``tusk -json`` is the surviving channel for programmatic
    one-off queries.
    """
    try:
        result = subprocess.run(
            [tusk_bin, "-json",
             f"SELECT COALESCE(domain, '') AS domain FROM tasks WHERE id = {task_id}"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    try:
        rows = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return ""
    if not rows or not isinstance(rows, list):
        return ""
    return (rows[0] or {}).get("domain", "") or ""


def _normalize_path_for_match(path: str, repo_root: str = "") -> str:
    """Return ``path`` as a forward-slash, repo-root-relative string.

    Patterns in path_test_commands are authored as repo-root-relative globs
    (``apps/scraper/*``), but callers sometimes pass absolute paths to
    ``tusk commit`` — the commit path-resolution branch at lines 473-479
    stores those absolute paths in ``resolved_files`` unchanged.  Without
    this normalization an input like ``/Users/.../repo/apps/scraper/foo.py``
    would never match ``apps/scraper/*`` and path_test_commands would
    silently fall through to domain_test_commands / test_command.
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


def match_path_test_command(patterns: dict, paths, repo_root: str = "") -> str:
    """Return the first path_test_commands entry whose pattern matches every path.

    Iterates patterns in insertion order (Python dicts preserve this since 3.7)
    and picks the first key whose fnmatch pattern matches *every* repo-root-
    relative path in ``paths``.  Absolute paths are converted to repo-root-
    relative form using ``repo_root`` before matching so callers can pass
    whatever ``resolved_files`` yielded (mix of absolute + relative entries).
    When staged changes span multiple subtrees and no single pattern covers
    them all, returns "" so the caller falls through to domain_test_commands
    / test_command.  Users encode a catch-all with
    ``"*": "<project-wide command>"`` at the end of the map.

    An empty-string command value disables that pattern — resolution falls
    through to the next entry as if the pattern were absent.  (This is the
    same idiom supported for domain_test_commands.)

    fnmatch's ``*`` matches across path separators, so ``apps/scraper/**`` and
    ``apps/scraper/*`` both match ``apps/scraper/foo/bar.py``.
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


def any_path_matches_test_surface(patterns: dict, paths, repo_root: str = "") -> bool:
    """Return True when at least one path touches a configured test surface."""
    if not patterns or not paths:
        return False
    normalized = [_normalize_path_for_match(p, repo_root) for p in paths]
    for pattern, cmd in patterns.items():
        if not cmd or not isinstance(cmd, str):
            continue
        if any(fnmatch.fnmatchcase(p, pattern) for p in normalized):
            return True
    return False


def load_test_command(config_path: str, domain: str = "", paths=None,
                      repo_root: str = "") -> str:
    """Load the effective test command from config.

    Resolution order:
      1. path_test_commands — first pattern where *every* path in ``paths``
         matches (see match_path_test_command for semantics).  ``repo_root``
         lets the matcher normalize absolute paths to repo-root-relative
         form before matching.
      2. domain_test_commands[domain] — when the task has a domain and a
         matching entry exists.
      3. Global test_command.

    Returns an empty string when no command is configured.
    """
    try:
        with open(config_path) as f:
            config = json.load(f)
        if paths:
            path_patterns = config.get("path_test_commands", {}) or {}
            cmd = match_path_test_command(path_patterns, paths, repo_root)
            if cmd:
                return cmd
            if (
                config.get("path_test_commands_skip_unmatched") is True
                and path_patterns
                and not any_path_matches_test_surface(path_patterns, paths, repo_root)
            ):
                return ""
        if domain:
            cmd = config.get("domain_test_commands", {}).get(domain)
            if cmd:
                return cmd
        return config.get("test_command", "") or ""
    except Exception:
        return ""


DEFAULT_TEST_COMMAND_TIMEOUT_SEC = 240
AUTO_TIMEOUT_SAMPLE_COUNT = 20
AUTO_TIMEOUT_MULTIPLIER = 2.0
# Bimodal/high-variance guard (issue #1062): the auto-scaled ceiling is also
# floored at the slowest recent successful run times this grace factor, so a
# history dominated by warm runs cannot yield a p95*multiplier ceiling that
# sits below a legitimate cold/under-load run already observed to succeed.
AUTO_TIMEOUT_MAX_RECENT_GRACE = 1.5
# When an auto-scaled timeout is hit on a run that was still producing output
# (progressing, not a silent hang), the gate retries the command once with the
# ceiling widened by this factor before aborting (issue #1062).
AUTO_TIMEOUT_RETRY_MULTIPLIER = 2.0

def _resolve_db_path(repo_root: str) -> str:
    """Best-effort DB path lookup mirroring bin/tusk's resolution.

    bin/tusk invokes tusk-commit.py with the CWD-resolved repo_root but does
    not pass DB_PATH explicitly. Reconstruct it here so the auto-scale layer
    and persistence helper can find the right database. Order matches the
    bash side: TUSK_DB > TUSK_REPO_ROOT/tusk/tasks.db > repo_root/tusk/tasks.db.
    """
    env_db = os.environ.get("TUSK_DB")
    if env_db:
        return env_db
    project_root = os.environ.get("TUSK_REPO_ROOT") or repo_root
    return os.path.join(project_root, "tusk", "tasks.db")


def _compute_auto_timeout(
    db_path: str,
    test_command: str,
    sample_count: int = AUTO_TIMEOUT_SAMPLE_COUNT,
    multiplier: float = AUTO_TIMEOUT_MULTIPLIER,
    floor: int | None = None,
    max_recent_grace: float = AUTO_TIMEOUT_MAX_RECENT_GRACE,
) -> int | None:
    """Return the auto-scaled timeout for ``test_command`` or None.

    The ceiling is ``max(static_floor, ceil(p95 * multiplier), ceil(max_recent
    * max_recent_grace))`` over the last N successful runs. The max-recent term
    is the issue #1062 bimodal/high-variance guard: when a history dominated by
    warm runs yields a low p95, ``p95 * multiplier`` can sit below a legitimate
    cold/under-load run that already succeeded, so the ceiling is also floored
    at the slowest recent success plus a grace margin.

    Cold start: returns None when fewer than ``sample_count`` successful runs
    of this exact ``test_command`` exist in the ``test_runs`` table — caller
    falls through to the static default. Scoping by the literal command
    string keeps a `pytest` history from contaminating a later
    `pytest -n auto` config.

    ``floor`` clamps the scaled value from below and defaults to the commit
    test gate's static default; the pre-merge lint gate passes its own 60s
    floor (issue #1070) so a fast lint history never produces a timeout
    tighter than the static default.

    The caller decides what to do with the result: this helper never raises;
    a missing DB, missing table (pre-migration install), schema mismatch, or
    locked DB all return None so the commit path stays advisory.
    """
    if not test_command or not os.path.exists(db_path):
        return None
    try:
        conn = open_sqlite(db_path, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT elapsed_seconds FROM test_runs "
                "WHERE test_command = ? AND succeeded = 1 "
                "ORDER BY id DESC LIMIT ?",
                (test_command, sample_count),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if len(rows) < sample_count:
        return None
    samples = sorted(float(r[0]) for r in rows)
    # p95 of N samples: index = ceil(0.95 * N) - 1, clamped to a valid index.
    idx = max(0, math.ceil(0.95 * len(samples)) - 1)
    p95 = samples[idx]
    if floor is None:
        floor = DEFAULT_TEST_COMMAND_TIMEOUT_SEC
    max_recent_floor = math.ceil(samples[-1] * max_recent_grace)
    return max(floor, math.ceil(p95 * multiplier), max_recent_floor)


def _record_test_run(
    db_path: str,
    task_id: int | None,
    test_command: str,
    elapsed_seconds: float,
    succeeded: bool = True,
) -> None:
    """Best-effort persistence of one test_command timing sample.

    Silent on any error — the auto-scale layer is advisory infrastructure and
    must never abort a commit. If the insert fails (DB locked, missing
    table on a pre-migration install, etc.), auto-scale just stays
    cold-started until the next successful write.
    """
    if not test_command or not os.path.exists(db_path):
        return
    try:
        conn = open_sqlite(db_path, timeout=2.0)
        try:
            conn.execute(
                "INSERT INTO test_runs (task_id, test_command, elapsed_seconds, succeeded) "
                "VALUES (?, ?, ?, ?)",
                (task_id, test_command, float(elapsed_seconds), 1 if succeeded else 0),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# Window within which a recorded test-precheck verdict is eligible for reuse by
# the commit test gate. The HEAD sha already pins the exact committed state, so
# a same-HEAD pre_existing verdict stays valid as long as HEAD has not moved;
# the window only bounds how stale a verdict the gate will trust (issue #1083).
PRECHECK_VERDICT_REUSE_WINDOW_SEC = 24 * 3600


def _reuse_precheck_verdict(
    db_path: str,
    repo_root: str,
    test_command: str,
    exit_code: int,
) -> str | None:
    """Return a bypass note when a same-HEAD pre_existing verdict exists, else None.

    Looks up the most recent ``precheck_verdicts`` row for the current HEAD sha
    and this exact ``test_command``. The verdict is reused only when
    ``pre_existing = 1`` and the row was written within
    ``PRECHECK_VERDICT_REUSE_WINDOW_SEC``. On a match, returns the audit note
    the caller stamps into the commit message body so the bypass is durable in
    git history (issue #1083).

    Best-effort and conservative: a missing DB, missing table (pre-migration
    install), locked DB, unresolvable HEAD, no row, a pre_existing=0 verdict, or
    a stale row all return None so the caller's exit-2 refusal stays intact.
    Refusing to bypass is always the safe default.
    """
    if not test_command or not os.path.exists(db_path):
        return None
    head = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo_root)
    if head.returncode != 0 or not head.stdout.strip():
        return None
    head_sha = head.stdout.strip()
    try:
        conn = open_sqlite(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT pre_existing, "
                "(julianday('now') - julianday(created_at)) * 86400 AS age_sec "
                "FROM precheck_verdicts "
                "WHERE head_sha = ? AND test_command = ? "
                "ORDER BY id DESC LIMIT 1",
                (head_sha, test_command),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    pre_existing, age_sec = row
    if not pre_existing:
        return None
    if age_sec is None or age_sec > PRECHECK_VERDICT_REUSE_WINDOW_SEC:
        return None
    return (
        f"[test-precheck-bypass] test_command exited {exit_code} but a same-HEAD "
        f"test-precheck verdict (HEAD {head_sha[:12]}) proved these failures "
        f"pre-existing; commit test gate bypassed."
    )


_TEST_COMMAND_ROUTING_ENV_KEYS = ("TUSK_DB", "TUSK_PROJECT", "TUSK_REPO_ROOT")
_TUSK_INTERNAL_PYTEST_IGNORE_GLOBS = (
    "test_tusk_*.py",
    "tests/test_tusk_*.py",
    "*/tests/test_tusk_*.py",
)


def _install_role() -> str:
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install-mode")
    try:
        value = open(marker, encoding="utf-8").read().strip()
    except OSError:
        return "source"
    if "-" not in value:
        return "source"
    return value.split("-", 1)[1]


def _add_consumer_pytest_quarantine(env: dict[str, str]) -> None:
    """Ignore stale vendored tusk framework tests during consumer test gates."""
    if _install_role() != "consumer":
        return
    ignore_opts = " ".join(
        f"--ignore-glob={glob}" for glob in _TUSK_INTERNAL_PYTEST_IGNORE_GLOBS
    )
    existing = env.get("PYTEST_ADDOPTS", "").strip()
    env["PYTEST_ADDOPTS"] = f"{existing} {ignore_opts}".strip()


def _test_command_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env for test_command subprocesses without tusk routing pins."""
    env = dict(os.environ if base_env is None else base_env)
    for key in _TEST_COMMAND_ROUTING_ENV_KEYS:
        env.pop(key, None)
    _add_consumer_pytest_quarantine(env)
    return env


def load_test_command_timeout(
    config_path: str,
    db_path: str | None = None,
    test_command: str | None = None,
) -> tuple[int, str]:
    """Return (timeout_seconds, source) for the test_command subprocess.

    Resolution order:
      1. TUSK_TEST_COMMAND_TIMEOUT env var (must parse as a positive int)
      2. config["test_command_timeout_sec"] (must parse as a positive int)
      3. Auto-scale from p95 of the last AUTO_TIMEOUT_SAMPLE_COUNT successful
         runs of this exact test_command (only when db_path and test_command
         are provided, AND the test_runs table holds enough samples)
      4. DEFAULT_TEST_COMMAND_TIMEOUT_SEC (240)

    source is one of: "env", "config", "auto", "default". Invalid values at
    any layer fall through to the next layer — the timeout is advisory
    infrastructure, not worth aborting the commit over a bad config value.

    Cold-start behavior: when fewer than AUTO_TIMEOUT_SAMPLE_COUNT successful
    samples exist for ``test_command``, the auto layer returns no value and
    the resolver falls through to the static default. Each successful run
    appends a row, so a fresh repo crosses the threshold after N healthy
    commits. Older callers that omit ``db_path``/``test_command`` skip the
    auto layer entirely and behave identically to the pre-auto resolver.
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
    if db_path is not None and test_command:
        auto_val = _compute_auto_timeout(db_path, test_command)
        if auto_val is not None:
            return auto_val, "auto"
    return DEFAULT_TEST_COMMAND_TIMEOUT_SEC, "default"


def is_linked_worktree(repo_root: str) -> bool:
    """Return True when repo_root is a linked git worktree checkout.

    In the primary checkout, `git rev-parse --git-dir` and
    `--git-common-dir` resolve to the same path. In a linked worktree they
    diverge: .git points at the per-worktree admin dir while common-dir points
    at the shared repository metadata.
    """
    try:
        git_dir = run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            check=False,
            cwd=repo_root,
        )
        common_dir = run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=False,
            cwd=repo_root,
        )
    except Exception:
        return False

    if git_dir.returncode != 0 or common_dir.returncode != 0:
        return False
    git_dir_path = git_dir.stdout.strip()
    common_dir_path = common_dir.stdout.strip()
    return bool(git_dir_path and common_dir_path and git_dir_path != common_dir_path)


def _current_branch(repo_root: str) -> str | None:
    """Return the current branch name, or None when HEAD is detached or git is unavailable."""
    result = run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _local_default_branch(repo_root: str) -> str:
    """Detect the repo's default branch using only local refs (no network).

    Order: refs/remotes/origin/HEAD symbolic-ref → local 'main' → local 'master'
    → literal 'main'. Used by branch validation only — keep it cheap.
    """
    remote_head = run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        check=False,
        cwd=repo_root,
    )
    if remote_head.returncode == 0:
        ref = remote_head.stdout.strip()
        if ref.startswith("origin/"):
            return ref[len("origin/"):]
        if ref:
            return ref
    for candidate in ("main", "master"):
        verify = run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"],
            check=False,
            cwd=repo_root,
        )
        if verify.returncode == 0:
            return candidate
    return "main"


_LOOKUP_UNAVAILABLE = "unavailable"
_LOOKUP_NO_ROW = "no_row"
_LOOKUP_FOUND = "found"


def _lookup_task_workspace(
    db_path: str, task_id: int
) -> tuple[str, tuple[str, str] | None]:
    """Return (status, payload) for the task's recorded workspace.

    status is one of:
      - ``_LOOKUP_UNAVAILABLE`` — DB file missing, table missing (pre-migration),
        or any sqlite error. Caller must NOT refuse on this signal: a test
        fixture or pre-init repo legitimately has no DB, and a transient read
        failure should never produce a false refusal.
      - ``_LOOKUP_NO_ROW`` — DB is healthy and queryable but the task has no
        recorded workspace. This is the signal that justifies the
        no-workspace-on-default refusal.
      - ``_LOOKUP_FOUND`` — payload is (branch, workspace_path).
    """
    if not os.path.exists(db_path):
        return (_LOOKUP_UNAVAILABLE, None)
    try:
        conn = open_sqlite(db_path, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT branch, workspace_path FROM task_workspaces "
                "WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return (_LOOKUP_UNAVAILABLE, None)
    if row is None:
        return (_LOOKUP_NO_ROW, None)
    return (_LOOKUP_FOUND, (row[0], row[1]))


def _validate_task_branch(
    repo_root: str,
    task_id: int,
    allow_mismatch: bool,
) -> tuple[bool, str | None]:
    """Pre-flight check: current branch must match the task's recorded workspace.

    Returns (True, None) on accept. Returns (False, diagnostic) on refusal.
    The diagnostic is a multi-line error message ready for ``_print_error``.

    Accept paths (never refuse):
      - ``--allow-branch-mismatch`` was passed.
      - HEAD is detached or git is unavailable (let downstream git ops handle).
      - The DB or ``task_workspaces`` table is unavailable.
      - Task has no recorded workspace AND current branch is non-default
        (legacy ``tusk branch`` flow).
      - Task has a recorded workspace AND current branch matches it.

    Refuse paths:
      - Recorded workspace exists but current branch differs.
      - No recorded workspace AND current branch is the default branch.
    """
    if allow_mismatch:
        return True, None
    current = _current_branch(repo_root)
    if current is None:
        return True, None
    db_path = _resolve_db_path(repo_root)
    status, payload = _lookup_task_workspace(db_path, task_id)
    if status == _LOOKUP_UNAVAILABLE:
        # DB missing / table missing / sqlite error — pre-init repo or test
        # fixture. Cannot validate, so do not block.
        return True, None
    if status == _LOOKUP_FOUND:
        expected_branch, workspace_path = payload  # type: ignore[misc]
        if current == expected_branch:
            return True, None
        diagnostic = (
            f"Error: tusk commit refusing — current branch does not match the task's recorded workspace.\n"
            f"  Task #{task_id} workspace branch: {expected_branch}\n"
            f"  Recorded workspace path:   {workspace_path}\n"
            f"  Current branch in {repo_root}: {current}\n"
            f"  Hint: switch into the task workspace before committing:\n"
            f"    cd {workspace_path}\n"
            f"  Or switch the current checkout to the expected branch:\n"
            f"    git switch {expected_branch}\n"
            f"  To override (legitimate cross-branch commits), pass --allow-branch-mismatch."
        )
        return False, diagnostic
    # status == _LOOKUP_NO_ROW: DB healthy, no workspace recorded for this task.
    default_branch = _local_default_branch(repo_root)
    if current == default_branch:
        diagnostic = (
            f"Error: tusk commit refusing to land on the default branch '{current}'.\n"
            f"  Task #{task_id} has no recorded task workspace.\n"
            f"  Hint: create a task workspace first:\n"
            f"    tusk task-worktree create {task_id} <slug>\n"
            f"  Or, for the legacy single-checkout flow, create a feature branch:\n"
            f"    tusk branch {task_id} <slug>\n"
            f"  To override (legitimate manual cleanup commits), pass --allow-branch-mismatch."
        )
        return False, diagnostic
    return True, None


# Test-runner summary signatures. When any of these appear in the captured
# output, the suite demonstrably RAN — a nonzero exit is a genuine test
# failure, not an unavailable command, no matter what substrings the test
# output happens to contain (issue #1067: a failing vitest assertion about a
# "not found" page was misrouted to the unavailable diagnostic, steering
# agents toward --skip-verify with a real regression in play).
_TEST_RUNNER_OUTPUT_RE = re.compile(
    "|".join(
        [
            # pytest final summary / section banners
            r"^=+ [^\n]*(?:passed|failed|error|no tests ran)[^\n]*=+\s*$",
            r"^\s*Test Files\s+\d",          # vitest
            r"^\s*Tests:?\s+\d",             # jest / vitest
            r"\b\d+ passing\b",              # mocha
            r"\b\d+ failing\b",              # mocha
            r"^--- (?:FAIL|PASS|SKIP):",     # go test
            r"^(?:FAIL|ok)\t",               # go test package line
            r"^test result: (?:ok|FAILED)",  # cargo test
            r"^(?:not )?ok \d+ ",            # TAP
        ]
    ),
    re.MULTILINE,
)

# Line-anchored shell-execution-error signatures: the SHELL itself (or env)
# reporting that a command could not be executed. Anchoring to a shell-name
# prefix keeps arbitrary test/assertion output (which may legitimately
# contain phrases like "not found") from matching.
_SHELL_EXEC_ERROR_RE = re.compile(
    r"^(?:/[\w./-]+/)?(?:sh|bash|zsh|dash|ksh)(?:\[\d+\])?:\s*"
    r"(?:(?:line )?\d+:\s*)?[^\n]*(?:command not found|No such file or directory|not found)\s*$"
    r"|^env:\s[^\n]*No such file or directory\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _test_command_unavailable(result: subprocess.CompletedProcess) -> bool:
    """Return True when the shell could not execute the configured command.

    Exit 126/127 is the shell's own cannot-exec signal and is authoritative.
    For other nonzero exits, recognizable test-runner output proves the suite
    ran (genuine test failure — return False), and only a line-anchored
    shell-execution-error signature on stderr counts as unavailable. Bare
    substring matches anywhere in captured test output do NOT qualify
    (issue #1067).
    """
    if result.returncode in (126, 127):
        return True
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    if _TEST_RUNNER_OUTPUT_RE.search(combined):
        return False
    return bool(_SHELL_EXEC_ERROR_RE.search(result.stderr or ""))


def _sparse_checkout_active(repo_root: str) -> bool:
    """Return True when sparse-checkout is enabled in ``repo_root``."""
    result = subprocess.run(
        ["git", "-C", repo_root, "config", "--get", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _shell_scan_tokens(command: str) -> list[str]:
    """Return shell-ish tokens suitable for conservative path scanning."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _path_materialization_tokens(test_cmd: str) -> list[str]:
    """Tokenize ``test_cmd``, peeling simple shell -c wrappers."""
    tokens = _shell_scan_tokens(test_cmd)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok in {"sh", "bash", "zsh", "dash", "ksh"}
            and i + 2 < len(tokens)
            and tokens[i + 1] == "-c"
        ):
            out.extend(_path_materialization_tokens(tokens[i + 2]))
            i += 3
            continue
        out.append(tok)
        i += 1
    return out


def _test_command_outside_sparse_cone(
    test_cmd: str, repo_root: str
) -> tuple[bool, str]:
    """Return ``(True, target)`` when ``test_cmd``'s path-shaped tokens all
    resolve outside the current sparse-checkout cone (i.e. they don't exist
    on disk in ``repo_root``). Used by the test gate to degrade gracefully
    instead of hard-failing the commit (TASK-480 criterion 2229, issue #906).

    Returns ``(False, "")`` when no path-shaped tokens were found, or when
    at least one resolves to an existing path. ``target`` is the first
    missing token, for inclusion in the info message.
    """
    targets = []
    tokens = _path_materialization_tokens(test_cmd)
    skip_next = False
    skip_until_separator = False
    expect_cd_target = False
    separators = {"|", "||", "&", "&&", ";"}
    shell_keywords = {
        "if", "then", "else", "elif", "fi", "do", "done", "while",
        "until", "for", "case", "esac", "!", "test", "[", "]",
    }
    for tok in tokens:
        tok = tok.strip().strip('"').strip("'")
        if skip_next:
            skip_next = False
            continue
        if tok in separators:
            skip_until_separator = False
            expect_cd_target = False
            continue
        if skip_until_separator:
            continue
        if not tok or tok.startswith("-") or "=" in tok:
            continue
        if tok in shell_keywords:
            continue
        if tok in {"echo"}:
            skip_until_separator = True
            continue
        if tok == "cd":
            expect_cd_target = True
            continue
        if tok in {">", ">>", "<", "<<", "<>", ">|"} or (
            tok[:-1].isdigit() and tok[-1:] in {">", "<"}
        ):
            skip_next = True
            continue
        if tok in {"&>", ">&"}:
            skip_next = True
            continue
        if re.match(r"^\d*(?:>>?|<<?|<>|>\|).*$", tok):
            continue
        if tok.startswith(("$", "~")) or "$" in tok:
            continue
        if os.path.isabs(tok) or tok.startswith("../"):
            continue
        if "://" in tok:
            continue
        if expect_cd_target or "/" in tok:
            targets.append(tok)
            expect_cd_target = False
    if not targets:
        return False, ""
    for tok in targets:
        if os.path.exists(os.path.join(repo_root, tok.rstrip("/"))):
            return False, ""
    return True, targets[0]


def _sparse_checkout_recovery_cone(paths: list[str], repo_root: str) -> list[str]:
    """Infer cone entries to add for paths rejected by sparse-checkout."""
    cones: list[str] = []
    seen: set[str] = set()
    for path in paths:
        rel = os.path.relpath(path, repo_root) if os.path.isabs(path) else path
        rel = os.path.normpath(rel)
        if rel.startswith(f".{os.sep}"):
            rel = rel[2:]
        if not rel or rel == "." or rel.startswith("../"):
            continue
        cone = rel.split(os.sep, 1)[0]
        if cone not in seen:
            cones.append(cone)
            seen.add(cone)
    return cones


def _render_tusk_commit_retry(
    task_id: int,
    message: str,
    files: list[str],
    criteria_ids: list[str],
    *,
    skip_verify: bool,
    skip_lint: bool,
    allow_branch_mismatch: bool,
    verbose: bool,
) -> str:
    """Render the equivalent tusk commit command using parsed arguments."""
    args = ["tusk", "commit", str(task_id), message, *files]
    if criteria_ids:
        args.append("--criteria")
        args.extend(criteria_ids)
    if skip_verify:
        args.append("--skip-verify")
    if skip_lint:
        args.append("--skip-lint")
    if allow_branch_mismatch:
        args.append("--allow-branch-mismatch")
    if verbose:
        args.append("--verbose")
    return " ".join(shlex.quote(arg) for arg in args)


def _render_sparse_checkout_recovery(
    paths: list[str],
    repo_root: str,
    task_id: int,
    message: str,
    files: list[str],
    criteria_ids: list[str],
    *,
    skip_verify: bool,
    skip_lint: bool,
    allow_branch_mismatch: bool,
    verbose: bool,
) -> str | None:
    """Return an actionable recovery command for sparse-checkout git-add errors."""
    cones = _sparse_checkout_recovery_cone(paths, repo_root)
    if not cones:
        return None
    add_cmd = " ".join(
        ["git", "sparse-checkout", "add", *[shlex.quote(cone) for cone in cones]]
    )
    retry_cmd = _render_tusk_commit_retry(
        task_id,
        message,
        files,
        criteria_ids,
        skip_verify=skip_verify,
        skip_lint=skip_lint,
        allow_branch_mismatch=allow_branch_mismatch,
        verbose=verbose,
    )
    return f"{add_cmd} && {retry_cmd}"


# Canonical non-code metadata files — mirrors scope.always_allowed in
# config.default.json. Used as the fallback when a project's tusk/config.json
# predates the scope.always_allowed key (or omits it), so VERSION / CHANGELOG /
# MANIFEST commits are still recognized as non-code (issue #950).
_DEFAULT_NON_CODE_FILES = (
    "VERSION",
    "CHANGELOG.md",
    "MANIFEST",
    ".claude/tusk-manifest.json",
)


def _resolve_non_code_allowlist(config_path: str) -> set[str]:
    """Return the set of repo-root-relative paths treated as non-code metadata
    files for the test-gate skip (issue #950).

    A project's ``scope.always_allowed`` wins when defined and non-empty;
    otherwise the canonical ``_DEFAULT_NON_CODE_FILES`` set is used so installs
    predating that config key still recognize VERSION / CHANGELOG / MANIFEST.
    """
    vals: list[str] = []
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            scope_cfg = cfg.get("scope")
            if isinstance(scope_cfg, dict):
                raw = scope_cfg.get("always_allowed")
                if isinstance(raw, list):
                    vals = [str(v) for v in raw if isinstance(v, str) and v]
        except (OSError, json.JSONDecodeError):
            vals = []
    if not vals:
        vals = list(_DEFAULT_NON_CODE_FILES)
    return {v.replace(os.sep, "/") for v in vals}


def _pending_commit_paths(repo_root: str, resolved_files) -> list[str]:
    """Return the repo-root-relative paths this commit will actually contain.

    ``tusk commit`` finalizes with a path-less ``git commit``, so the commit
    captures more than the explicitly-resolved files: any already-staged index
    changes (modifications, additions, or ``git rm`` deletions). The non-code
    test-gate skip must reason over this full set so a pre-staged code file
    never bypasses the gate (issue #950). Unstaged changes outside the explicit
    path list are intentionally excluded because this command does not stage
    them (issue #1212).
    """
    paths = {
        os.path.relpath(f, repo_root) if os.path.isabs(f) else f
        for f in resolved_files
    }
    res = run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        check=False,
        cwd=repo_root,
    )
    if res.returncode == 0 and res.stdout:
        paths.update(p for p in res.stdout.split("\0") if p)
    return list(paths)


def _listed_paths_missing_from_head(repo_root: str, resolved_files) -> list[str]:
    """Return explicitly listed, existing paths absent from HEAD's tree.

    A listed path that no longer exists on disk may be a legitimate staged
    deletion or rename source, so this check only covers non-deletion paths.
    """
    expected = []
    for f in resolved_files:
        rel = os.path.relpath(f, repo_root) if os.path.isabs(f) else f
        abs_path = f if os.path.isabs(f) else os.path.join(repo_root, f)
        if os.path.exists(abs_path):
            expected.append(rel)
    if not expected:
        return []

    missing = []
    for path in expected:
        res = run(["git", "cat-file", "-e", f"HEAD:{path}"], check=False, cwd=repo_root)
        if res.returncode != 0:
            missing.append(path)
    return missing


def _is_github_workflow_yaml(norm_path: str) -> bool:
    lower = norm_path.lower()
    return lower.startswith(".github/workflows/") and lower.endswith((".yml", ".yaml"))


def _all_staged_files_non_code(rel_paths, allowlist: set[str]) -> bool:
    """Return True when every path in ``rel_paths`` is a non-code file that
    cannot change test outcomes — a Markdown/docs file (``*.md``), a GitHub
    workflow YAML file, or a ``scope.always_allowed`` metadata entry (VERSION,
    MANIFEST, CHANGELOG.md, .claude/tusk-manifest.json). Used to skip the test
    gate for docs-only / workflow-only / version-bump commits (issue #950).

    Empty input returns False: with nothing to reason about, the caller's
    normal (gate-runs) path is the safe default.
    """
    if not rel_paths:
        return False
    for rel in rel_paths:
        norm = str(rel).replace(os.sep, "/")
        if norm in allowlist:
            continue
        if norm.lower().endswith(".md"):
            continue
        if _is_github_workflow_yaml(norm):
            continue
        return False
    return True


def _dump_timeout_output(exc: subprocess.TimeoutExpired, verbose: bool) -> None:
    """Dump partial stdout/stderr captured before a timed-out child was killed.

    With ``capture_output=True`` the TimeoutExpired carries whatever was
    collected before the kill; even with ``text=True`` the buffered payload can
    come back as raw bytes when the timeout fires before decode, so handle both
    shapes. No-op in verbose mode where output already streamed live.
    """
    if verbose:
        return
    if exc.stdout:
        out = exc.stdout
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        sys.stdout.write(out)
        sys.stdout.flush()
    if exc.stderr:
        err = exc.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        sys.stderr.write(err)
        sys.stderr.flush()


def _timeout_source_hint(timeout_source: str) -> str:
    """Human-readable description of where the test_command timeout came from."""
    return {
        "env": "TUSK_TEST_COMMAND_TIMEOUT env var",
        "config": 'config key "test_command_timeout_sec"',
        "auto": 'auto-scaled from p95 of recent successful runs '
                '(override with "test_command_timeout_sec" in tusk/config.json '
                'or TUSK_TEST_COMMAND_TIMEOUT env var)',
        "default": 'default (override with "test_command_timeout_sec" in tusk/config.json '
                   'or TUSK_TEST_COMMAND_TIMEOUT env var)',
    }[timeout_source]


def _timeout_had_progress(exc: subprocess.TimeoutExpired) -> bool:
    """True when the timed-out run produced any output.

    A run that emitted test output before the kill was progressing (likely just
    slow on a cold/under-load host), not hung waiting for input — the signal
    that gates the issue #1062 auto-retry.
    """
    return bool(exc.stdout) or bool(exc.stderr)


def _run_test_with_retry(
    test_cmd: str,
    repo_root: str,
    timeout_sec: int,
    timeout_source: str,
    verbose: bool,
) -> tuple[subprocess.CompletedProcess | None, float | None]:
    """Run ``test_cmd`` once, auto-retrying a progressing auto-timeout once.

    Returns ``(completed_process, elapsed_seconds)`` on completion (the
    returncode may be nonzero — that's the caller's normal failure path), or
    ``(None, None)`` when the command timed out terminally and the caller should
    abort with exit 5 (the diagnostic has already been printed to stderr).

    Issue #1062: an auto-scaled ceiling is only an estimate. When it is hit on a
    run that was STILL PRODUCING OUTPUT (progressing, not a silent hang), the
    estimate was likely too tight for a cold/under-load run, so the command is
    retried once with the ceiling widened by AUTO_TIMEOUT_RETRY_MULTIPLIER. The
    retry fires only for the ``auto`` source — explicit env/config/default
    ceilings are intentional and respected as-is — and only on a progressing
    timeout, so a genuine silent hang aborts after the first ceiling.
    """
    started = time.monotonic()
    try:
        test = subprocess.run(
            test_cmd,
            shell=True,
            capture_output=not verbose,
            text=True, encoding="utf-8",
            cwd=repo_root,
            env=_test_command_env(),
            timeout=timeout_sec,
        )
        return test, time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        _dump_timeout_output(exc, verbose)
        if timeout_source == "auto" and _timeout_had_progress(exc):
            retry_timeout = math.ceil(timeout_sec * AUTO_TIMEOUT_RETRY_MULTIPLIER)
            print(
                f"\nNote: test_command hit the auto-scaled timeout "
                f"({timeout_sec}s) while still producing output — retrying once "
                f"with a widened ceiling ({retry_timeout}s) before aborting "
                f"(issue #1062).",
                file=sys.stderr,
            )
            sys.stderr.flush()
            started = time.monotonic()
            try:
                test = subprocess.run(
                    test_cmd,
                    shell=True,
                    capture_output=not verbose,
                    text=True, encoding="utf-8",
                    cwd=repo_root,
                    env=_test_command_env(),
                    timeout=retry_timeout,
                )
                return test, time.monotonic() - started
            except subprocess.TimeoutExpired as exc2:
                _dump_timeout_output(exc2, verbose)
                _print_error(
                    f"\nError: test_command timed out again after {retry_timeout}s "
                    f"on the auto-retry — aborting commit\n"
                    f"  Command: {test_cmd}\n"
                    f"  Timeout source: {_timeout_source_hint(timeout_source)} "
                    f"(retried once with a widened ceiling, issue #1062)\n"
                    f"  Hint: if the command needs more time, raise the limit; "
                    f"if it hangs waiting for input (e.g. interactive mode), "
                    f"switch to a non-interactive form."
                )
                return None, None
        _print_error(
            f"\nError: test_command timed out after {timeout_sec}s — aborting commit\n"
            f"  Command: {test_cmd}\n"
            f"  Timeout source: {_timeout_source_hint(timeout_source)}\n"
            f"  Hint: if the command needs more time, raise the limit; "
            f"if it hangs waiting for input (e.g. interactive mode), switch to a non-interactive form."
        )
        return None, None


def _print_test_command_failure(
    result: subprocess.CompletedProcess,
    test_cmd: str,
    elapsed: float,
    repo_root: str,
) -> None:
    """Emit the most actionable failure message for the test_command gate."""
    if _test_command_unavailable(result) and is_linked_worktree(repo_root):
        print(
            "\nError: configured test_command is unavailable in this linked worktree "
            f"(exit {result.returncode}, {elapsed:.1f}s)",
            file=sys.stderr,
        )
        print(f"  Command: {test_cmd}", file=sys.stderr)
        print(
            "  This usually means the global test_command references tools or paths "
            "that are not present in this worktree checkout.",
            file=sys.stderr,
        )
        print(
            "  Next steps: configure a narrower path_test_commands entry "
            "matching the staged paths, or a domain_test_commands entry for "
            "this task's domain; or rerun with --skip-verify if you already "
            "ran the relevant targeted verification intentionally.",
            file=sys.stderr,
        )
        return

    print(
        f"\nError: test_command failed (exit {result.returncode}, {elapsed:.1f}s) — aborting commit",
        file=sys.stderr,
    )


# Reject commit messages containing shell-substitution metacharacters that zsh
# and bash expand BEFORE tusk sees the argv — backticks, $(...), ${...}, and
# bare $VAR. The boundary guard fires before any git/sqlite subprocess so a
# substituted message never lands on origin (issue #881; original incident
# TASK-464 shipped a JSON blob into commit 984ca1a/main when a literal
# `tusk sync-main` inside a double-quoted message got executed by zsh).
# Commit-message-specific remedy line. The metacharacter regex and diagnostic
# shape live in the shared reject_shell_metacharacters helper (tusk-git-helpers)
# so tusk commit and the task-text surfaces (issue #1106) stay in lockstep; this
# wrapper keeps the legacy name and the commit-message wording.
_COMMIT_METACHAR_REMEDY = (
    "Fix: rewrite the message without the metacharacter (use plain "
    "identifiers, not backticked code spans). If you need to describe "
    "literal shell syntax, write it in words instead of including the "
    "shell metacharacter."
)


def _validate_message_metacharacters(message: str) -> tuple[bool, str]:
    """Return (True, "") when the message is safe, else (False, diagnostic).

    Thin wrapper over the shared guard: rejects any backtick (`), $(...) command
    substitution, ${...} braced variable substitution, or bare $<identifier>
    substitution. The agent's intended literal must be rewritten without
    metacharacters (plain identifiers) — auto-escaping would silently mutate the
    message and is deliberately not offered (issue #881).
    """
    return _git_helpers.reject_shell_metacharacters(
        message, subject="commit message", remedy=_COMMIT_METACHAR_REMEDY
    )


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
            "Usage: tusk commit <task_id> \"<message>\" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify] [--allow-branch-mismatch] [--verbose]",
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
    allow_branch_mismatch: bool = False
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
        elif remaining[i] == "--allow-branch-mismatch":
            allow_branch_mismatch = True
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
                "Usage: tusk commit <task_id> <file1> [file2 ...] -m \"<message>\" [--criteria <id>] ... [--skip-verify] [--allow-branch-mismatch]",
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
                "Usage: tusk commit <task_id> \"<message>\" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify] [--allow-branch-mismatch]",
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

    ok, diagnostic = _validate_message_metacharacters(message)
    if not ok:
        print(diagnostic, file=sys.stderr)
        return 1

    # ── Announce status lines? ───────────────────────────────────────
    # Status banners ("starting TASK-N", "=== Running tests ===",
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
    #   Step 0a (branch validation) → exit 7  (current branch does not match
    #                                          recorded task workspace; bypass
    #                                          with --allow-branch-mismatch)
    #   Step 0  (path validation)   → exit 3  (escapes root or path not found)
    #   Step 1a (index preflight)   → exit 3  (git index lock unavailable)
    #   Step 1b (test_command gate) → exit 2  (test_command failed)
    #   Step 2  (git add)           → exit 3  (git add failed)
    #   Step 3  (git commit)        → exit 3  (git commit failed)
    #   Step 4  (criteria done)     → exit 4  (one or more criteria failed)
    #   Argument / validation errors before Step 0 → exit 1

    # ── Step 0a: Validate current branch matches the task's recorded workspace ─
    # Refuses when the operator is committing from a branch that does not match
    # the task_workspaces row, or from the default branch when no workspace is
    # recorded (issue #794). Bypass with --allow-branch-mismatch when intentional.
    ok, diagnostic = _validate_task_branch(repo_root, task_id, allow_branch_mismatch)
    if not ok:
        _print_error(diagnostic)
        return 7

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

    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")

    if skip_lint and announce_status:
        print("Note: --skip-lint is ignored by tusk commit; lint runs at merge time.")

    # ── Step 1a: Preflight git index writability before expensive gates ─
    index_ok, index_diagnostic = _preflight_git_index_writable(repo_root)
    if not index_ok:
        _print_error(index_diagnostic)
        return 3

    # ── Step 1b: Run test_command gate (hard-blocks on failure) ──────
    # Only query the task's domain when domain_test_commands is configured —
    # avoids a DB round-trip for the common case where domain routing is unused.
    # resolved_files is passed through so path_test_commands (insertion-order
    # glob → command) can take precedence over domain/global when a single
    # pattern covers every staged path.
    task_domain = ""
    try:
        with open(config_path) as _f:
            _cfg = json.load(_f)
        if _cfg.get("domain_test_commands"):
            task_domain = load_task_domain(tusk_bin, task_id)
    except Exception:
        pass
    test_cmd = load_test_command(
        config_path, task_domain, resolved_files, repo_root=real_repo_root,
    )
    # Sparse-checkout-aware preflight (TASK-480 criterion 2229, issue #906).
    # If the worktree is sparse-checked-out and every path-shaped token in
    # the test command resolves to a non-existent path on disk, the test
    # command would fail with "file or directory not found" — that's an
    # environment problem, not a regression. Info-skip the gate instead of
    # hard-failing (which previously forced every commit in the session to
    # use --skip-verify, bypassing tests and pre-commit hooks).
    sparse_skip_test = False
    if test_cmd and not skip_verify and _sparse_checkout_active(repo_root):
        outside, missing_target = _test_command_outside_sparse_cone(
            test_cmd, repo_root
        )
        if outside:
            print(
                f"Note: test_command path '{missing_target}' is not "
                f"materialized under the current sparse-checkout cone — "
                f"skipping test gate for this commit.\n"
                f"  Command: {test_cmd}\n"
                f"  Recover (optional): extend the cone with "
                f"`git sparse-checkout add {missing_target.rstrip('/')}`."
            )
            sys.stdout.flush()
            sparse_skip_test = True
    # Non-code-only preflight (issue #950). When every staged file is a
    # docs/markdown file, GitHub workflow YAML file, or scope.always_allowed
    # metadata file (VERSION, CHANGELOG.md, MANIFEST,
    # .claude/tusk-manifest.json), the commit cannot change test outcomes —
    # running the full test gate is wasted wall-clock and needlessly exposes the
    # (recommended) VERSION-bump-as-own-commit path to timeout flakes under
    # load. Info-skip the gate; lint (Step 1) and pre-commit hooks (Step 3)
    # still run since they are separate steps. Preserve always-run behavior
    # whenever any code file is staged.
    noncode_skip_test = False
    if test_cmd and not skip_verify and not sparse_skip_test:
        gate_rel_paths = _pending_commit_paths(repo_root, resolved_files)
        if _all_staged_files_non_code(
            gate_rel_paths, _resolve_non_code_allowlist(config_path)
        ):
            print(
                "Note: every staged file is non-code (docs/markdown, GitHub "
                "workflow YAML, or a scope.always_allowed metadata file) — "
                "skipping test gate "
                "for this commit.\n"
                f"  Staged: {', '.join(gate_rel_paths)}\n"
                "  These files cannot change test outcomes; lint and "
                "pre-commit hooks still run."
            )
            sys.stdout.flush()
            noncode_skip_test = True
    # Set when the test gate fails but a same-HEAD test-precheck verdict proves
    # the failures pre-existing — stamped into the commit message body so the
    # bypass is durable in git history (issue #1083).
    gate_bypass_note: str | None = None
    if test_cmd and not skip_verify and not sparse_skip_test and not noncode_skip_test:
        test_cmd, _ = _worktree_command.rewrite_linked_worktree_venv_command(
            test_cmd,
            repo_root,
        )
        db_path = _resolve_db_path(repo_root)
        timeout_sec, timeout_source = load_test_command_timeout(
            config_path, db_path, test_cmd,
        )
        # Only announce the command up-front in verbose mode.  In quiet mode
        # (the default) we keep stdout short so background-task callers can find
        # the final summary line with `tail -1` instead of scrolling through
        # 300KB of pytest output.
        if verbose:
            print(f"=== Running test_command: {test_cmd} (timeout {timeout_sec}s) ===")
            sys.stdout.flush()
        test, elapsed = _run_test_with_retry(
            test_cmd, repo_root, timeout_sec, timeout_source, verbose,
        )
        if test is None:
            # Terminal timeout (after the optional auto-retry); the diagnostic
            # was already printed by _run_test_with_retry.
            return 5
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
            gate_bypass_note = _reuse_precheck_verdict(
                db_path, repo_root, test_cmd, test.returncode,
            )
            if gate_bypass_note is None:
                _print_test_command_failure(test, test_cmd, elapsed, repo_root)
                return 2
            print(
                f"\nNote: test_command failed (exit {test.returncode}, "
                f"{elapsed:.1f}s) but a same-HEAD test-precheck verdict proved "
                "these failures pre-existing — proceeding with commit "
                "(bypass recorded in the commit message).",
            )
            sys.stdout.flush()
        else:
            print(f"tests passed ({elapsed:.1f}s)")
            sys.stdout.flush()
            _record_test_run(db_path, task_id, test_cmd, elapsed, succeeded=True)

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
                    recovery = _render_sparse_checkout_recovery(
                        to_add,
                        repo_root,
                        task_id,
                        message,
                        files,
                        criteria_ids,
                        skip_verify=skip_verify,
                        skip_lint=skip_lint,
                        allow_branch_mismatch=allow_branch_mismatch,
                        verbose=verbose,
                    )
                    if recovery:
                        _print_error(
                            "  Hint: one or more files are outside the git "
                            "sparse-checkout cone."
                        )
                        _print_error(f"  Run: {recovery}")
                    else:
                        _print_error(
                            "  Hint: one or more files are outside the git "
                            "sparse-checkout cone — run `git sparse-checkout add "
                            "<directory>` to include them."
                        )

        if stderr_text is not None:
            return 3

    # ── Step 4: Commit ───────────────────────────────────────────────
    if announce_status:
        print("=== Creating commit ===")
        sys.stdout.flush()
    body_extra = f"\n\n{gate_bypass_note}" if gate_bypass_note else ""
    full_message = f"[TASK-{task_id}] {message}{body_extra}\n\n{TRAILER}"
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

    missing_from_tree = _listed_paths_missing_from_head(repo_root, resolved_files)
    if missing_from_tree:
        for path in missing_from_tree:
            _print_error(
                f"Error: explicitly listed path is missing from the committed tree: {path}"
            )
        _print_error(
            "Hint: the path may have been skipped during staging. Stage the file "
            "manually, then retry tusk commit."
        )
        return 3

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
        criteria_env = os.environ.copy()
        if test_cmd and not skip_verify and state.get("sha"):
            criteria_env["TUSK_COMMIT_GATE_COMMAND"] = test_cmd
            criteria_env["TUSK_COMMIT_GATE_SHA"] = state["sha"]
        result = subprocess.run(
            cmd,
            capture_output=False,
            check=False,
            env=criteria_env,
        )
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
