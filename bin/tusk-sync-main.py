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
  2. `git diff --name-only --diff-filter=U` — if any paths are unmerged, exit
     early with a structured diagnostic naming the offending files. Every
     state-mutating step that follows (fetch, stash, ff-merge, pop, migrate)
     would otherwise hit the opaque `git stash push failed: could not write
     index` failure mode (issue #914) — the user-actionable signal is "resolve
     the conflict," not "stash failed."
  3. `git fetch origin <default>` so commit counts and ff-pull use fresh refs.
  4. Count commits to be pulled: `git rev-list HEAD..origin/<default> --count`.
     Zero means we are already up to date — skip pull but still run migrate
     (a previous fetch may have left pending schema changes).
  5. If the working tree is dirty, push a uniquely-named stash entry and
     look up the ref by message — same pattern as tusk-test-precheck.py, so
     concurrent invocations do not collide and we never pop by stash position.
  6. `git merge --ff-only origin/<default>` to fast-forward. If this fails
     after a stash was created, restore that stash by its looked-up ref before
     surfacing the git error and exiting non-zero.
  7. If we stashed, pop the entry by its looked-up ref.
  8. Run `tusk migrate` to apply any schema migrations the new commits brought.

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
import re
import subprocess
import sys
import tempfile
import time
import uuid

# Transient .git/index.lock contention signatures (issue #1075). "could not
# write index" is what `git stash pop` emits when another process holds the
# lock mid-write; the "Unable to create ... index.lock" wording is the same
# class seen by tusk-merge's _run_with_index_lock_retry (issues #620, #640).
# Real pop conflicts match neither, so they are never retried.
_TRANSIENT_INDEX_LOCK_RE = re.compile(
    r"could not write index|Unable to create '[^']*\.git/index\.lock'"
)
_POP_LOCK_BACKOFF_SECONDS = (0.5, 1.0)


def _run(cmd, cwd, check=False, env=None):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        check=check, env=env,
    )


def _parse_merge_tree_conflicts(output):
    """Extract conflicted paths from ``git merge-tree --write-tree`` output.

    Layout (conflict case): line 0 is the toplevel tree OID; every subsequent
    line up to the first blank line is a conflicted-file entry in
    ``<mode> <oid> <stage>\\t<path>`` form. The informational ``CONFLICT (...)``
    messages follow the blank line and are ignored here.
    """
    lines = output.splitlines()
    paths = []
    seen = set()
    for line in lines[1:]:
        if not line.strip():
            break
        if "\t" not in line:
            continue
        path = line.split("\t", 1)[1].strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _preflight_stash_conflict(repo_root, default_branch):
    """Detect whether the dirty tree would conflict with the incoming
    origin/<default> commits BEFORE stashing (issue #1095).

    ``git stash pop`` runs after the fast-forward, so a conflict between the
    local changes and the incoming commits surfaces only once the tree is
    already half-rewritten — the partial-apply hybrid state TASK-643 could
    only explain after the fact. This pre-flight performs the same 3-way
    merge ahead of time: it builds a throwaway tree from a TEMP index
    (``read-tree HEAD`` + ``add -A`` → HEAD plus every working change,
    including untracked) and asks ``git merge-tree`` to merge
    origin/<default> against it with HEAD as the base — exactly what the pop
    will do. Only the object DB is written; the real index and working tree
    are never touched, so an abort leaves the operator's state pristine.

    Returns:
      - ``list[str]`` of conflicted paths when a conflict is detected,
      - ``[]`` when the merge is clean,
      - ``None`` when the check could not be performed (any git step failed,
        empty output, or a merge-tree too old to support ``--write-tree``) so
        the caller falls through to the normal stash/pop path rather than
        blocking on an unprovable state.
    """
    tmp_index = None
    try:
        fd, tmp_index = tempfile.mkstemp(prefix="tusk-sync-preflight-index-")
        os.close(fd)
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
        if _run(["git", "read-tree", "HEAD"], cwd=repo_root, env=env).returncode != 0:
            return None
        if _run(["git", "add", "-A"], cwd=repo_root, env=env).returncode != 0:
            return None
        tree_res = _run(["git", "write-tree"], cwd=repo_root, env=env)
        local_tree = tree_res.stdout.strip()
        if tree_res.returncode != 0 or not local_tree:
            return None
        commit_res = _run(
            ["git", "commit-tree", local_tree, "-p", "HEAD",
             "-m", "tusk-sync-main preflight"],
            cwd=repo_root,
        )
        local_commit = commit_res.stdout.strip()
        if commit_res.returncode != 0 or not local_commit:
            return None
        merge_res = _run(
            ["git", "merge-tree", "--write-tree", "--merge-base=HEAD",
             f"origin/{default_branch}", local_commit],
            cwd=repo_root,
        )
        if merge_res.returncode == 0:
            return []
        if merge_res.returncode != 1:
            return None  # indeterminate (e.g. merge-tree predates --write-tree)
        return _parse_merge_tree_conflicts(merge_res.stdout)
    finally:
        if tmp_index and os.path.exists(tmp_index):
            os.unlink(tmp_index)


def _pop_stash_with_lock_retry(repo_root, current_ref):
    """Pop the stash; retry briefly on transient index.lock contention.

    A concurrent session's git process can hold .git/index.lock at the moment
    of the pop (issue #1075) — the same pop succeeds seconds later with zero
    conflicts. Retries len(_POP_LOCK_BACKOFF_SECONDS) times; any failure that
    does not match the lock signature returns immediately.
    """
    pop_res = _run(["git", "stash", "pop", current_ref], cwd=repo_root)
    total = len(_POP_LOCK_BACKOFF_SECONDS)
    for attempt, delay in enumerate(_POP_LOCK_BACKOFF_SECONDS, start=1):
        if pop_res.returncode == 0 or not _TRANSIENT_INDEX_LOCK_RE.search(
            pop_res.stderr or ""
        ):
            return pop_res
        print(
            f"Note: git stash pop hit transient .git/index.lock contention; "
            f"retrying ({attempt}/{total}) after {delay}s...",
            file=sys.stderr,
        )
        time.sleep(delay)
        pop_res = _run(["git", "stash", "pop", current_ref], cwd=repo_root)
    return pop_res


def _format_pop_failure(repo_root, current_ref, stash_message, pop_res):
    """Build the stderr message for a failed stash pop.

    A conflicted pop PARTIALLY applies: git stages every cleanly-merged file
    (polluting the index with the operator's previously-unstaged WIP) and
    leaves the conflicted file(s) in UU state, while keeping the stash entry
    (issue #1063). That state needs explicit step-by-step recovery — the
    operator's original state was unstaged WIP, and improvising the
    index-restoration inside user-owned uncommitted work is risky. Conflict
    lines land on stdout, so classification scans stdout+stderr.
    """
    combined = (pop_res.stdout or "") + (pop_res.stderr or "")
    if "CONFLICT" not in combined:
        return (
            f"Error: git stash pop {current_ref} failed: "
            f"{pop_res.stderr.strip()}\nYour changes remain in the stash "
            f"list under message '{stash_message}'."
        )
    try:
        unmerged = _unmerged_paths(repo_root)
    except RuntimeError:
        unmerged = []
    if unmerged:
        conflicted = "Conflicted file(s): " + ", ".join(unmerged)
    else:
        conflicted = "Conflicted file(s): see `git status` (UU entries)"
    return (
        f"Error: git stash pop {current_ref} hit a merge conflict and "
        "PARTIALLY applied: git staged every cleanly-merged file (your "
        "previously-unstaged changes are now in the index) and left the "
        f"conflicted file(s) in UU state. {conflicted}.\n"
        f"The stash entry is kept under message '{stash_message}'.\n"
        "To restore your original unstaged-WIP state:\n"
        "  1. Resolve the conflict markers in the UU file(s), then `git add` them\n"
        "  2. git reset    # unstage everything the pop staged — your WIP was unstaged before sync-main\n"
        f"  3. git stash drop {current_ref}    # the kept entry is now redundant"
    )


def _restore_stash_after_merge_failure(repo_root, stash_message):
    """Restore the stash this sync-main invocation created before ff-merge failed."""
    try:
        current_ref = _find_stash_ref(repo_root, stash_message)
    except RuntimeError as e:
        print(
            f"Error: {e}\nYour changes remain in the stash list under message "
            f"'{stash_message}'. Pop it manually with `git stash list` + "
            "`git stash pop <ref>`.",
            file=sys.stderr,
        )
        return False
    if not current_ref:
        print(
            f"Error: stash entry '{stash_message}' disappeared before it could "
            "be restored. Inspect `git stash list` and `git fsck --lost-found`.",
            file=sys.stderr,
        )
        return False
    pop_res = _pop_stash_with_lock_retry(repo_root, current_ref)
    if pop_res.returncode != 0:
        print(
            _format_pop_failure(repo_root, current_ref, stash_message, pop_res),
            file=sys.stderr,
        )
        return False
    print(
        f"Note: restored stashed local changes from {current_ref} after "
        "ff-only merge failure.",
        file=sys.stderr,
    )
    return True


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


def _unmerged_paths(repo_root):
    """Return list of paths with unresolved merge conflicts (git status UU/AA/...)."""
    result = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_root
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff --diff-filter=U failed: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _parse_name_status_paths(output):
    paths = []
    seen = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1].strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _parse_porcelain_status(output):
    statuses = {}
    for line in output.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            statuses[path] = status
    return statuses


def _blob_at(repo_root, ref, path):
    result = _run(["git", "rev-parse", f"{ref}:{path}"], cwd=repo_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _worktree_blob(repo_root, path):
    result = _run(["git", "hash-object", "--", path], cwd=repo_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _stale_restored_paths(repo_root, old_head, new_head):
    """Return dirty paths that look restored from the pre-fast-forward tree.

    ``git stash pop`` can apply cleanly yet leave a path at the old HEAD's blob
    while the fast-forwarded HEAD contains newer content. That creates WIP that
    looks legitimate but would commit an accidental revert.
    """
    if not old_head or not new_head or old_head == new_head:
        return []

    incoming = _run(
        ["git", "diff", "--name-status", f"{old_head}..{new_head}"],
        cwd=repo_root,
    )
    if incoming.returncode != 0:
        return []
    changed = set(_parse_name_status_paths(incoming.stdout))
    if not changed:
        return []

    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo_root,
    )
    if status.returncode != 0:
        return []
    dirty = _parse_porcelain_status(status.stdout)

    stale = []
    for path in sorted(changed.intersection(dirty)):
        old_blob = _blob_at(repo_root, old_head, path)
        new_blob = _blob_at(repo_root, new_head, path)
        if old_blob == new_blob:
            continue
        status_code = dirty[path]
        if "D" in status_code:
            if not old_blob and new_blob:
                stale.append(path)
            continue
        worktree_blob = _worktree_blob(repo_root, path)
        if old_blob and worktree_blob == old_blob:
            stale.append(path)
    return stale


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

    try:
        unmerged = _unmerged_paths(repo_root)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1, result
    if unmerged:
        if len(unmerged) <= 10:
            display = ", ".join(unmerged)
        else:
            display = ", ".join(unmerged[:10]) + f", ... and {len(unmerged) - 10} more"
        print(
            f"Error: primary has {len(unmerged)} unmerged path(s) "
            f"({display}) — resolve them before tusk sync-main.",
            file=sys.stderr,
        )
        return 1, result

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

    old_head = ""
    if result["fetched_commits"] > 0 and dirty:
        head_res = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
        if head_res.returncode != 0 or not head_res.stdout.strip():
            print(
                f"Error: git rev-parse HEAD failed: {head_res.stderr.strip()}",
                file=sys.stderr,
            )
            return 1, result
        old_head = head_res.stdout.strip()

    # Pre-flight: refuse BEFORE stashing when the local changes would conflict
    # with the incoming commits, so the working tree is left untouched instead
    # of landing in the half-applied stash-pop state TASK-643 could only
    # explain after the fact (issue #1095). Best-effort and signal-gated: an
    # indeterminate result (None) falls through to the normal stash/pop path.
    if (
        result["fetched_commits"] > 0
        and dirty
        and not os.environ.get("TUSK_SYNC_MAIN_NO_PREFLIGHT")
    ):
        conflicts = _preflight_stash_conflict(repo_root, default_branch)
        if conflicts:
            if len(conflicts) <= 10:
                display = ", ".join(conflicts)
            else:
                display = (
                    ", ".join(conflicts[:10])
                    + f", ... and {len(conflicts) - 10} more"
                )
            print(
                f"Error: local changes would conflict with the "
                f"{result['fetched_commits']} incoming commit(s) from "
                f"origin/{default_branch} in {len(conflicts)} file(s) "
                f"({display}). Aborting before stashing so your working tree is "
                "left untouched. Commit or revert the conflicting change(s) and "
                "retry, or sync manually and resolve the stash-pop conflict. "
                "(Set TUSK_SYNC_MAIN_NO_PREFLIGHT=1 to skip this check.)",
                file=sys.stderr,
            )
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
            if result["stashed"]:
                _restore_stash_after_merge_failure(repo_root, stash_message)
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
        pop_res = _pop_stash_with_lock_retry(repo_root, current_ref)
        if pop_res.returncode != 0:
            print(
                _format_pop_failure(repo_root, current_ref, stash_message, pop_res),
                file=sys.stderr,
            )
            return 1, result
        head_res = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
        if head_res.returncode != 0 or not head_res.stdout.strip():
            print(
                f"Error: git rev-parse HEAD failed after stash pop: "
                f"{head_res.stderr.strip()}",
                file=sys.stderr,
            )
            return 1, result
        stale_paths = _stale_restored_paths(repo_root, old_head, head_res.stdout.strip())
        if stale_paths:
            if len(stale_paths) <= 10:
                display = ", ".join(stale_paths)
            else:
                display = (
                    ", ".join(stale_paths[:10])
                    + f", ... and {len(stale_paths) - 10} more"
                )
            print(
                f"Error: git stash pop restored stale file snapshot(s) in "
                f"{len(stale_paths)} path(s) ({display}). These paths now match "
                "the pre-sync HEAD while the fast-forwarded HEAD has newer "
                "content, so committing them could create an accidental revert. "
                "Inspect the files and restore any stale paths before committing.",
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

    ap = argparse.ArgumentParser(allow_abbrev=False, prog="tusk sync-main")
    ap.parse_args(rest)

    tusk_bin = os.path.join(os.path.dirname(os.path.realpath(__file__)), "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"

    exit_code, payload = sync_main(repo_root, tusk_bin)
    print(json.dumps(payload))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
