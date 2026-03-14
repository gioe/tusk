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
    2. Run test_command from config if set and --skip-verify not passed (hard-blocks on failure)
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
    return subprocess.run(args, capture_output=True, text=True, check=check, cwd=cwd)


def load_test_command(config_path: str) -> str:
    """Load test_command from config, defaulting to empty string (disabled)."""
    try:
        with open(config_path) as f:
            config = json.load(f)
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
            # Always call realpath (not guarded by os.path.exists) so that
            # case-mismatched path components are normalised even for new files
            # that don't exist on disk yet.  realpath resolves all existing
            # ancestors to their canonical form and passes through any
            # non-existing tail unchanged (strict=False default behaviour).
            real_abs = os.path.realpath(abs_path)
            if _escapes_root(real_abs, real_repo_root):
                escape_errors.append((f, abs_path))
            resolved_files.append(os.path.relpath(abs_path, repo_root))

    if escape_errors:
        for orig, abs_path in escape_errors:
            print(
                f"Error: path escapes the repo root: '{orig}'\n"
                f"  Resolved to: '{abs_path}'\n"
                f"  Repo root is: {repo_root}\n"
                f"  Hint: paths must be inside the repo root",
                file=sys.stderr,
            )
        return 3

    # Pre-flight: verify each resolved path exists so we can emit a useful diagnostic
    # before git produces a cryptic "pathspec did not match" error.
    missing = [
        (orig, resolved)
        for orig, resolved in zip(files, resolved_files)
        if not os.path.exists(resolved if os.path.isabs(resolved) else os.path.join(repo_root, resolved))
    ]
    if missing:
        for orig, resolved in missing:
            was_remapped = orig != resolved
            if not was_remapped:
                print(
                    f"Error: path not found: '{orig}'\n"
                    f"  Hint: paths must exist relative to the repo root ({repo_root})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: path not found: '{orig}'\n"
                    f"  Resolved to (repo-root-relative): '{resolved}'\n"
                    f"  Hint: the file was not found at {os.path.join(repo_root, resolved)}",
                    file=sys.stderr,
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
    test_cmd = load_test_command(config_path)
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
        files_str = " ".join(resolved_files)
        print(
            f"Error: git add failed (cwd: {repo_root}):\n"
            f"  Command: git add -- {files_str}\n"
            f"  {stderr_text}",
            file=sys.stderr,
        )
        # Probe each file with git check-ignore -v to surface the specific
        # gitignore rule (if any) blocking it — more actionable than checking
        # for English substrings in git's locale-dependent error output.
        ignored_files = []
        for f in resolved_files:
            ci = run(["git", "check-ignore", "-v", f], check=False, cwd=repo_root)
            if ci.returncode == 0 and ci.stdout.strip():
                ignored_files.append((f, ci.stdout.strip()))
        if ignored_files:
            for f, rule in ignored_files:
                print(
                    f"  Gitignore rule blocking '{f}':\n"
                    f"    {rule}\n"
                    f"  Hint: use `git add -f {f}` to force-add, then commit manually.",
                    file=sys.stderr,
                )
        elif "ignored by" in stderr_text or ".gitignore" in stderr_text:
            # Fallback: git reported gitignore but check-ignore didn't find the rule
            print(
                "  Hint: one or more files are excluded by .gitignore — "
                "use `git add -f <file>` to force-add, then commit manually.",
                file=sys.stderr,
            )
        elif "sparse-checkout" in stderr_text:
            print(
                "  Hint: one or more files are outside the git sparse-checkout cone — "
                "run `git sparse-checkout add <directory>` to include them.",
                file=sys.stderr,
            )
        return 3

    # ── Step 4: Commit ───────────────────────────────────────────────
    full_message = f"[TASK-{task_id}] {message}\n\n{TRAILER}"
    # Capture HEAD before committing so we can verify whether the commit
    # landed even when a hook (e.g. husky + lint-staged) exits non-zero.
    pre = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo_root)
    pre_sha = pre.stdout.strip() if pre.returncode == 0 else None

    result = run(["git", "commit", "-m", full_message], check=False, cwd=repo_root)

    if result.returncode != 0:
        # Check whether the commit actually landed despite the non-zero exit.
        post = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo_root)
        post_sha = post.stdout.strip() if post.returncode == 0 else None
        commit_landed = post_sha and post_sha != pre_sha

        if not commit_landed:
            print(f"Error: git commit failed:\n{result.stderr.strip()}", file=sys.stderr)
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
