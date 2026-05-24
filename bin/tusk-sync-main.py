#!/usr/bin/env python3
"""tusk sync-main — fast-forward local default branch from origin and re-run migrate.

One-shot recovery for /address-issue Step 4.6's "Don't trust local source
files when origin may be ahead" case. The manual sequence is fetch +
optional stash + ff-only pull + optional pop + tusk migrate, each with its
own failure mode. This helper wraps the sequence so the operator does not
have to remember it.

Procedure:
  1. Resolve the default branch via `git symbolic-ref refs/remotes/origin/HEAD`
     (with a `tusk git-default-branch` fallback).
  2. `git fetch origin <default>` so commit counts and ff-pull use fresh refs.
  3. Count commits to be pulled: `git rev-list HEAD..origin/<default> --count`.
     Zero means we are already up to date — skip pull but still run migrate
     (a previous fetch may have left pending schema changes).
  4. If the working tree is dirty, push a uniquely-named stash entry and
     look up the ref by message — same pattern as tusk-test-precheck.py, so
     concurrent invocations do not collide and we never pop by stash position.
  5. `git merge --ff-only origin/<default>` to fast-forward. If this fails,
     leave the stash intact, surface the git error, and exit non-zero.
  6. If we stashed, pop the entry by its looked-up ref.
  7. Run `tusk migrate` to apply any schema migrations the new commits brought.

Output: a single JSON object on stdout:

    {
      "success": bool,
      "default_branch": str,
      "fetched_commits": int,
      "stashed": bool,
      "migrated": bool
    }

Exit codes:
  0  Success (success=true in JSON)
  1  Recoverable failure (success=false in JSON; stderr carries detail)
  2  Unrecoverable / usage error (no JSON written; stderr carries detail)
"""

import argparse
import json
import os
import subprocess
import sys
import uuid


def _run(cmd, cwd, check=False):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check
    )


def _resolve_default_branch(repo_root, tusk_bin):
    result = _run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo_root
    )
    if result.returncode == 0 and result.stdout.strip():
        ref = result.stdout.strip()
        if ref.startswith("origin/"):
            return ref[len("origin/"):]
        return ref
    fallback = _run([tusk_bin, "git-default-branch"], cwd=repo_root)
    if fallback.returncode == 0 and fallback.stdout.strip():
        return fallback.stdout.strip()
    return "main"


def _is_dirty(repo_root):
    result = _run(["git", "status", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def _find_stash_ref(repo_root, message):
    """Return stash ref whose subject contains *message*, or empty string.

    Raises RuntimeError if the underlying git command failed — callers must
    never conflate "command failed" with "no match", because doing so is the
    silent-data-loss path tusk-test-precheck.py exists to prevent.
    """
    result = _run(["git", "stash", "list", "--format=%gd %gs"], cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(f"git stash list failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        ref, _, subject = line.partition(" ")
        if message in subject:
            return ref
    return ""


def sync_main(repo_root, tusk_bin):
    """Run the full sync sequence. Returns (exit_code, result_dict)."""
    result = {
        "success": False,
        "default_branch": None,
        "fetched_commits": 0,
        "stashed": False,
        "migrated": False,
    }

    default_branch = _resolve_default_branch(repo_root, tusk_bin)
    result["default_branch"] = default_branch

    fetch_res = _run(["git", "fetch", "origin", default_branch], cwd=repo_root)
    if fetch_res.returncode != 0:
        print(
            f"Error: git fetch origin {default_branch} failed: "
            f"{fetch_res.stderr.strip()}",
            file=sys.stderr,
        )
        return 1, result

    count_res = _run(
        ["git", "rev-list", "--count", f"HEAD..origin/{default_branch}"], cwd=repo_root
    )
    if count_res.returncode == 0 and count_res.stdout.strip().isdigit():
        result["fetched_commits"] = int(count_res.stdout.strip())

    try:
        dirty = _is_dirty(repo_root)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1, result

    stash_message = ""
    if result["fetched_commits"] > 0 and dirty:
        stash_message = f"tusk-sync-main/{os.getpid()}/{uuid.uuid4().hex[:8]}"
        push_res = _run(
            ["git", "stash", "push", "--include-untracked", "-m", stash_message],
            cwd=repo_root,
        )
        if push_res.returncode != 0:
            print(
                f"Error: git stash push failed: {push_res.stderr.strip()}",
                file=sys.stderr,
            )
            return 1, result
        try:
            stash_ref = _find_stash_ref(repo_root, stash_message)
        except RuntimeError as e:
            print(
                f"Error: {e}\nStash entry '{stash_message}' was created but could "
                "not be verified. Inspect `git stash list` and pop it manually.",
                file=sys.stderr,
            )
            return 1, result
        if not stash_ref:
            print(
                f"Error: git stash push reported success but entry "
                f"'{stash_message}' is not in git stash list. Local changes "
                "may be in an inconsistent state.",
                file=sys.stderr,
            )
            return 1, result
        result["stashed"] = True

    if result["fetched_commits"] > 0:
        merge_res = _run(
            ["git", "merge", "--ff-only", f"origin/{default_branch}"], cwd=repo_root
        )
        if merge_res.returncode != 0:
            print(
                f"Error: git merge --ff-only origin/{default_branch} failed: "
                f"{merge_res.stderr.strip()}\n"
                f"Hint: the local branch has diverged from origin. Resolve with "
                f"`git pull --rebase origin {default_branch}` or `git reset --hard "
                f"origin/{default_branch}` if local commits are disposable.",
                file=sys.stderr,
            )
            return 1, result

    if result["stashed"]:
        try:
            current_ref = _find_stash_ref(repo_root, stash_message)
        except RuntimeError as e:
            print(
                f"Error: {e}\nYour changes remain in the stash list under message "
                f"'{stash_message}'. Pop it manually with `git stash list` + "
                "`git stash pop <ref>`.",
                file=sys.stderr,
            )
            return 1, result
        if not current_ref:
            print(
                f"Error: stash entry '{stash_message}' disappeared during the "
                "sync. Inspect `git stash list` and `git fsck --lost-found`.",
                file=sys.stderr,
            )
            return 1, result
        pop_res = _run(["git", "stash", "pop", current_ref], cwd=repo_root)
        if pop_res.returncode != 0:
            print(
                f"Error: git stash pop {current_ref} failed: "
                f"{pop_res.stderr.strip()}\nYour changes remain in the stash "
                f"list under message '{stash_message}'.",
                file=sys.stderr,
            )
            return 1, result

    migrate_res = _run([tusk_bin, "migrate"], cwd=repo_root)
    if migrate_res.returncode != 0:
        print(
            f"Error: tusk migrate failed: {migrate_res.stderr.strip()}\n"
            "The fast-forward and pop succeeded, but pending schema migrations "
            "were not applied. Re-run `tusk migrate` after resolving the issue.",
            file=sys.stderr,
        )
        return 1, result
    result["migrated"] = True
    result["success"] = True
    return 0, result


def main(argv):
    if not argv:
        print(
            "Usage: tusk-sync-main.py <repo_root>",
            file=sys.stderr,
        )
        return 2
    repo_root = argv[0]
    rest = argv[1:]

    ap = argparse.ArgumentParser(prog="tusk sync-main")
    ap.parse_args(rest)

    tusk_bin = os.path.join(os.path.dirname(os.path.realpath(__file__)), "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"

    exit_code, payload = sync_main(repo_root, tusk_bin)
    print(json.dumps(payload))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
