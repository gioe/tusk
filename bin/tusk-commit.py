#!/usr/bin/env python3
"""Lint, stage, and commit in one atomic operation.

Called by the tusk wrapper:
    tusk commit <task_id> "<message>" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify]

Arguments received from tusk:
    sys.argv[1] — repo root
    sys.argv[2] — config path
    sys.argv[3:] — task_id, message, files, and optional --criteria / --skip-verify flags

Steps:
    0. Validate file paths — fail fast before lint/tests if any path is missing or escapes repo root
    1. Run tusk lint (advisory — output is printed but never blocks)
    2. Run test_command gate: use domain_test_commands[task.domain] if present, else test_command (hard-blocks on failure)
    3. Stage files: git add for all files (handles additions, modifications, and deletions)
    4. git commit with [TASK-<id>] <message> format and Co-Authored-By trailer
    5. For each criterion ID passed via --criteria, call tusk criteria done <id> (captures HEAD automatically)

Exit codes:
    0 — success
    1 — usage or validation error (bad arguments, invalid task ID, etc.)
    2 — test_command failed (nothing was staged or committed)
    3 — git add or git commit failed
    4 — one or more criteria could not be marked done (commit itself succeeded)
"""

import json
import os
import subprocess
import sys


TRAILER = "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"


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


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(
            "Usage: tusk commit <task_id> \"<message>\" <file1> [file2 ...] [--criteria <id>] ... [--skip-verify]",
            file=sys.stderr,
        )
        return 1

    repo_root = argv[0]
    config_path = argv[1]
    remaining = argv[2:]

    # Parse --criteria and --skip-verify flags out of remaining args; collect everything else positionally
    criteria_ids: list[str] = []
    skip_verify: bool = False
    positional: list[str] = []
    i = 0
    while i < len(remaining):
        if remaining[i] == "--criteria":
            i += 1
            collected = 0
            while i < len(remaining) and not remaining[i].startswith("--"):
                criteria_ids.append(remaining[i])
                i += 1
                collected += 1
            if collected == 0:
                print("Error: --criteria requires at least one argument", file=sys.stderr)
                return 1
        elif remaining[i] == "--skip-verify":
            skip_verify = True
            i += 1
        else:
            positional.append(remaining[i])
            i += 1

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

    # Validate criteria IDs are integers
    for cid in criteria_ids:
        try:
            int(cid)
        except ValueError:
            print(f"Error: Invalid criterion ID: {cid}", file=sys.stderr)
            return 1

    if not message.strip():
        print("Error: Commit message must not be empty", file=sys.stderr)
        return 1

    # ── Startup sentinel ─────────────────────────────────────────────
    # Written to stdout immediately so that background-task output-file
    # capture has a non-empty file even when the process exits early.
    print(f"tusk commit: starting TASK-{task_id}", flush=True)

    # ── Step → exit-code map (quick reference for diagnosis) ─────────
    #   Step 0  (path validation)   → exit 3  (escapes root or path not found)
    #   Step 1  (lint)              → advisory only; never exits
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
        missing = [
            (orig, resolved)
            for (orig, resolved), rel in zip(not_on_disk, rel_for_git)
            if rel not in git_tracked
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

    # ── Step 1: Run lint (advisory) ──────────────────────────────────
    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")
    print("=== Running tusk lint (advisory) ===")
    lint = subprocess.run([tusk_bin, "lint"], capture_output=False)
    if lint.returncode != 0:
        print("\nLint reported warnings (advisory only — continuing)\n")
    else:
        print()

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
        print(f"=== Running test_command: {test_cmd} ===")
        test = subprocess.run(test_cmd, shell=True, capture_output=False, cwd=repo_root)
        if test.returncode != 0:
            print(
                f"\nError: test_command failed (exit {test.returncode}) — aborting commit",
                file=sys.stderr,
            )
            return 2
        print()

    # ── Step 3: Stage files ──────────────────────────────────────────
    # File paths were already resolved and validated in Step 0.
    # git add handles deletions of tracked files natively since Git 2.x — no git rm needed.
    # The -- separator prevents git from misinterpreting file paths as options.
    result = run(["git", "add", "--"] + resolved_files, check=False, cwd=repo_root)
    if result.returncode != 0:
        stderr_text = result.stderr.strip()

        # Special case: a hook (e.g. lint-staged) may have already staged these files,
        # leaving the working tree clean so git add finds nothing to update and exits
        # non-zero with "pathspec did not match any files".  If every requested file is
        # already present in the index, treat the add as a no-op and proceed to commit.
        if "pathspec" in stderr_text and "did not match" in stderr_text:
            rel_resolved = [
                os.path.relpath(f, repo_root) if os.path.isabs(f) else f
                for f in resolved_files
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
            files_str = " ".join(resolved_files)
            # Probe each file with git check-ignore -v to surface the specific
            # gitignore rule (if any) blocking it — more actionable than checking
            # for English substrings in git's locale-dependent error output.
            ignored_files = []
            for f in resolved_files:
                ci = run(["git", "check-ignore", "-v", f], check=False, cwd=repo_root)
                if ci.returncode == 0 and ci.stdout.strip():
                    ignored_files.append((f, ci.stdout.strip()))

            if ignored_files:
                # Auto-retry: directories like .claude/skills/ are gitignored but
                # have force-tracked siblings already in the index — git add -f is
                # the correct way to stage new files there (Issue #401).
                ignored_paths = [f for f, _ in ignored_files]
                non_ignored_paths = [f for f in resolved_files if f not in ignored_paths]
                print(
                    f"Note: {len(ignored_paths)} file(s) blocked by .gitignore — "
                    "retrying with `git add -f` (force-add for gitignored paths)."
                )
                retry_ok = True
                r_force = run(
                    ["git", "add", "-f", "--"] + ignored_paths, check=False, cwd=repo_root
                )
                if r_force.returncode != 0:
                    retry_ok = False
                    _print_error(f"Error: git add -f also failed:\n  {r_force.stderr.strip()}")
                if retry_ok and non_ignored_paths:
                    r_rest = run(
                        ["git", "add", "--"] + non_ignored_paths, check=False, cwd=repo_root
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
                    for f, rule in ignored_files:
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
                    # Fallback: git reported gitignore but check-ignore didn't find the rule
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

        if not commit_landed:
            error_text = result.stderr.strip()
            _print_error(f"Error: git commit failed:\n{error_text}")
            hook_keywords = ("lint-staged", "pre-commit", "husky", "hook")
            if any(kw in error_text.lower() for kw in hook_keywords):
                _print_error(
                    "  Hint: a pre-commit hook rejected the commit. "
                    "Run with --skip-verify to bypass hooks: "
                    "tusk commit ... --skip-verify"
                )
            else:
                _print_error(
                    "  Hint: if a pre-commit hook is causing this, "
                    "try: tusk commit ... --skip-verify"
                )
            return 3

        # Commit landed but a hook emitted a non-zero exit (e.g. lint-staged
        # "no staged files" warning). Surface it as a note, not a fatal error.
        warning = result.stderr.strip()
        if warning:
            print(f"Note: git hook warning (commit landed successfully):\n{warning}")

    if result.stdout.strip():
        print(result.stdout.strip())

    # ── Step 5: Mark criteria done (captures new HEAD automatically) ─
    # When multiple criteria are batched in one commit call, suppress the
    # shared-commit warning for criteria[1:] — the user intentionally grouped them.
    criteria_failed = False
    for idx, cid in enumerate(criteria_ids):
        print(f"\n=== Marking criterion {cid} done ===")
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
        print("Use: tusk commit <task_id> \"<message>\" <file1> [file2 ...]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
