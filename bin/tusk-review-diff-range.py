#!/usr/bin/env python3
"""Compute the git diff range for /review-commits against a task's branch.

Given a task_id, determine the most meaningful git diff range for the review:

    1. Primary range — ``origin/<default_branch>...HEAD`` when the
       remote-tracking default exists and is at least as current as the local
       default, otherwise ``<default_branch>...HEAD``. The default branch name
       is resolved by shelling out to ``tusk git-default-branch`` so the
       remote-HEAD → gh → "main" detection stays in lockstep with the wrapper.
    2. Fallback — if the primary range has an empty diff (e.g. the feature
       branch has already been merged into the default branch and deleted),
       scan ``git log`` for the 50 most recent commits whose message contains
       ``[TASK-<id>]`` and build a range ``<oldest>^..<newest>`` from that
       set. This mirrors Step 3 of ``/review-commits``.

If both paths yield an empty diff — no ``[TASK-<id>]`` commits found in
recent history, or the recovered range is still empty — exit non-zero with
an error message on stderr. The review cannot proceed without a diff.

Usage:
    tusk review-diff-range <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path (used only to resolve repo_root)
    sys.argv[2] — config path (unused)
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Output JSON (stdout on success):
    {
        "range": "<default>...HEAD" | "<sha>^..<sha>",
        "diff_lines": <int>,
        "diff_lines_meaningful": <int>,
        "summary": "<first 120 chars of git diff output>",
        "recovered_from_task_commits": <bool>,
        "resolved_repo_root": "<path>"
    }

``resolved_repo_root`` is the checkout against which the returned range
should be interpreted (issue #821 / TASK-412). The primary checkout's
``repo_root`` is used by default, but the worktree-fallback path may
re-resolve into a sibling worktree to find the feature branch; callers
that re-run ``git diff`` (e.g. ``tusk review validate-comments``) MUST
use the returned ``resolved_repo_root`` as ``cwd`` to keep the diff
consistent with the range.

``diff_lines`` counts every newline in the raw ``git diff`` output (legacy
field, unchanged for backward compatibility). ``diff_lines_meaningful``
subtracts the per-file sections of auto-generated lockfiles
(``package-lock.json``, ``yarn.lock``, ``pnpm-lock.yaml``, ``Cargo.lock``,
``go.sum``, and friends — see ``GENERATED_LOCKFILES`` in
``tusk-git-helpers.py``). Consumers driving inline-vs-agent routing
decisions (e.g. ``skills/review-commits/SKILL.md``) should prefer the
meaningful count so a single ``npm install`` does not push a small feature
into agent-based review (issue #761).

Exit codes:
    0 — success (JSON on stdout)
    1 — bad arguments, or no diff recoverable (error on stderr)
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py, tusk-git-helpers.py, tusk-db-lib.py

_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
_db_lib = tusk_loader.load("tusk-db-lib")
dumps = _json_lib.dumps
task_grep_arg = _git_helpers.task_grep_arg
find_task_commits = _git_helpers.find_task_commits
commit_changed_files = _git_helpers.commit_changed_files
task_referenced_paths = _git_helpers.task_referenced_paths
filter_lockfile_diff_sections = _git_helpers.filter_lockfile_diff_sections
get_connection = _db_lib.get_connection

SUMMARY_CHARS = 120
TASK_COMMIT_LIMIT = 50

_TUSK_WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")


def _git(args: list, repo_root: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )


def _git_stdout(args: list, repo_root: str | None = None) -> str | None:
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
    }
    if repo_root is not None:
        kwargs["cwd"] = repo_root
    r = subprocess.run(["git", *args], **kwargs)
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()


def _git_common_dir(repo_root: str) -> str | None:
    path = _git_stdout(["rev-parse", "--git-common-dir"], repo_root)
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(repo_root, path)
    return os.path.realpath(path)


def resolve_repo_root(db_path: str, cwd: str | None = None) -> str:
    """Resolve the git checkout whose HEAD should be reviewed.

    The DB always lives under the original project checkout, but task worktrees
    have their own HEAD. Prefer the caller's cwd when it is a worktree of the
    same git repository; otherwise fall back to the DB-derived checkout.
    """
    db_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    db_common = _git_common_dir(db_repo_root)
    if not db_common:
        return db_repo_root

    invocation_cwd = cwd or os.getcwd()
    candidate = _git_stdout(["rev-parse", "--show-toplevel"], invocation_cwd)
    if not candidate:
        return db_repo_root
    candidate = os.path.abspath(candidate)
    candidate_common = _git_common_dir(candidate)
    if candidate_common and candidate_common == db_common:
        return candidate
    return db_repo_root


def default_branch(repo_root: str) -> str:
    """Resolve the default branch by calling ``tusk git-default-branch``.

    Shelling out to the wrapper (rather than re-implementing the symbolic-ref
    → gh → "main" cascade here) keeps this helper in lockstep with every
    other caller of ``tusk git-default-branch`` across the codebase.
    """
    r = subprocess.run(
        [_TUSK_WRAPPER, "git-default-branch"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    branch = (r.stdout or "").strip()
    return branch or "main"


def _ref_exists(ref: str, repo_root: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", ref], repo_root).returncode == 0


def primary_range(base: str, repo_root: str) -> str:
    """Choose the best default-branch comparison ref for review diffs."""
    remote = f"origin/{base}"
    remote_exists = _ref_exists(remote, repo_root)
    if remote_exists:
        return f"{remote}...HEAD"
    return f"{base}...HEAD"


def _filter_commits_by_task_overlap(
    task_id: int, commits: list, repo_root: str, db_path: str | None
) -> list:
    """Drop commits whose file diff doesn't overlap this task's referenced paths.

    Mirrors the prefix-collision file-overlap heuristic wired into
    ``tusk merge`` (TASK-308) and ``tusk task-done`` (TASK-309). Without
    the filter, a stray ``[TASK-<id>]``-tagged commit (recycled task ID
    after a fresh DB init, fat-fingered commit message) would be folded
    into the diff range and the reviewer agent would assess unrelated
    code (issue #656).

    Skipped when the task has no scope signal (no referenced paths), or
    when the DB is unreachable — in either case there's no basis to
    discriminate, so every commit is returned. Order is preserved so
    callers can still take ``commits[0]`` as newest, ``commits[-1]`` as
    oldest.
    """
    if not commits or not db_path or not os.path.isfile(db_path):
        return list(commits)
    try:
        conn = get_connection(db_path)
    except Exception:
        return list(commits)
    try:
        task_paths = set(task_referenced_paths(task_id, conn))
    finally:
        conn.close()
    if not task_paths:
        return list(commits)
    return [sha for sha in commits if commit_changed_files([sha], repo_root) & task_paths]


def _task_started_at(db_path: str | None, task_id: int) -> str | None:
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        conn = get_connection(db_path)
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row:
        return None
    return row["started_at"] if "started_at" in row.keys() else row[0]


def _count_meaningful_lines(diff_out: str) -> int:
    """Return the newline count of *diff_out* after stripping every per-file
    section that belongs to an auto-generated lockfile (issue #761)."""
    if not diff_out:
        return 0
    return filter_lockfile_diff_sections(diff_out).count("\n")


def _find_task_feature_worktree(
    task_id: int, repo_root: str
) -> str | None:
    """Return the path of a sibling worktree whose branch matches
    ``feature/TASK-<id>-*``, excluding *repo_root* itself.

    Resolves issue #777: when ``tusk review begin`` is invoked from a
    checkout that has no ``[TASK-<id>]`` commits reachable from HEAD (the
    primary checkout, the wrong worktree, etc.) but the feature branch is
    checked out in a sibling worktree, the helper can re-run the diff
    against that worktree instead of failing with a generic "No changes
    found" message.

    Returns ``None`` when no matching sibling worktree exists or when
    ``git worktree list`` fails.
    """
    porcelain = _git(["worktree", "list", "--porcelain"], repo_root)
    if porcelain.returncode != 0 or not porcelain.stdout:
        return None

    invoking_real = os.path.realpath(repo_root)
    target_prefix = f"refs/heads/feature/TASK-{task_id}-"
    current_wt: str | None = None
    current_branch: str | None = None

    def _maybe_return() -> str | None:
        if (
            current_wt
            and current_branch
            and current_branch.startswith(target_prefix)
            and os.path.realpath(current_wt) != invoking_real
        ):
            return current_wt
        return None

    for line in porcelain.stdout.split("\n"):
        if not line.strip():
            hit = _maybe_return()
            if hit:
                return hit
            current_wt = None
            current_branch = None
            continue
        if line.startswith("worktree "):
            current_wt = line[len("worktree "):]
        elif line.startswith("branch "):
            current_branch = line[len("branch "):]

    return _maybe_return()


def _sibling_hint(task_id: int, repo_root: str) -> str:
    """Return a leading-space hint pointing at a sibling worktree, or ''.

    Used by the "No changes found" error paths so the operator hitting
    issue #817 from the primary checkout sees the feature-branch
    worktree path in the error instead of having to guess where the
    work actually lives.
    """
    sibling = _find_task_feature_worktree(task_id, repo_root)
    if not sibling:
        return ""
    return (
        f" Feature branch is checked out at sibling worktree '{sibling}'; "
        f"re-run from there with `cd '{sibling}' && tusk review begin {task_id}`."
    )


def compute_range(
    task_id: int,
    repo_root: str,
    db_path: str | None = None,
    _allow_worktree_fallback: bool = True,
) -> dict:
    """Return the diff-range payload for this task, or raise on empty diff.

    The ``_allow_worktree_fallback`` flag is internal — when both the
    primary range and the ``[TASK-N]`` commit-grep recovery come up empty,
    the helper consults ``git worktree list`` for a sibling worktree whose
    branch matches ``feature/TASK-<id>-*`` and recurses into it with the
    flag flipped off, so the fallback only fires once. See issue #777.
    """
    base = default_branch(repo_root)
    primary = primary_range(base, repo_root)

    primary_result = _git(["diff", primary], repo_root)
    if primary_result.returncode != 0 and not primary.startswith("origin/"):
        remote_primary = f"origin/{base}...HEAD"
        remote_result = _git(["diff", remote_primary], repo_root)
        if remote_result.returncode == 0:
            primary = remote_primary
            primary_result = remote_result

    diff_out = primary_result.stdout if primary_result.returncode == 0 else ""
    diff_lines = diff_out.count("\n") if diff_out else 0
    if diff_lines > 0:
        # Issue #821 / TASK-412: verify the chosen primary range actually
        # contains at least one [TASK-<id>] commit before accepting it.
        # When the orchestrator's CWD has unpushed local-default commits
        # unrelated to this task, the primary range is non-empty-but-wrong;
        # falling through to the commit-grep / worktree fallback recovers
        # the real feature-branch range.
        primary_task_commits = find_task_commits(
            task_id, repo_root, refs=[primary],
        )
        if primary_task_commits:
            return {
                "range": primary,
                "diff_lines": diff_lines,
                "diff_lines_meaningful": _count_meaningful_lines(diff_out),
                "summary": diff_out[:SUMMARY_CHARS],
                "recovered_from_task_commits": False,
                "resolved_repo_root": repo_root,
            }
        # Primary range is non-empty but contains no [TASK-N] commits.
        # Fall through to commit-grep recovery as if it were empty.

    # Primary range is empty — recover from [TASK-N] commits across all refs
    # (issue #817 / TASK-412). Scanning `--all` lets the primary checkout
    # discover commits on a sibling worktree's feature branch through the
    # shared git object database, without depending on HEAD-reachability or
    # on the secondary worktree-list fallback below.
    started_at = _task_started_at(db_path, task_id)
    commits = find_task_commits(
        task_id,
        repo_root,
        refs=["--all", "-n", str(TASK_COMMIT_LIMIT)],
        since=started_at,
    )
    if not commits:
        # Sibling-worktree fallback (issue #777): the feature branch may
        # live in another worktree (typical when `tusk task-worktree create`
        # set one up and the user invoked `tusk review begin` from the
        # primary checkout). Re-run compute_range against the discovered
        # worktree's repo_root once, then surface a clear hint if that
        # also turns up empty.
        if _allow_worktree_fallback:
            sibling = _find_task_feature_worktree(task_id, repo_root)
            if sibling:
                try:
                    return compute_range(
                        task_id,
                        sibling,
                        db_path,
                        _allow_worktree_fallback=False,
                    )
                except SystemExit:
                    raise SystemExit(
                        f"No changes found in checkout '{repo_root}' for "
                        f"TASK-{task_id}, and the sibling worktree at "
                        f"'{sibling}' also has no [TASK-{task_id}] commits. "
                        "Confirm the correct commit range manually and re-run."
                    )
        raise SystemExit(
            f"No changes found — [TASK-{task_id}] commits not detected in this "
            f"checkout's git log ('{repo_root}'). No sibling worktree carries "
            f"a feature/TASK-{task_id}-* branch either. The diff range cannot "
            "be determined automatically. Confirm the correct commit range "
            "manually and re-run."
        )

    # Prefix-collision file-overlap heuristic (issue #656): drop commits
    # whose file diff doesn't overlap with this task's referenced paths
    # before we hand the range to the reviewer agent.
    filtered = _filter_commits_by_task_overlap(task_id, commits, repo_root, db_path)
    if not filtered:
        raise SystemExit(
            f"No changes found — every [TASK-{task_id}] commit in recent git log "
            "touches files outside this task's referenced paths (prefix-match "
            "false positive, issue #656). The diff range cannot be determined "
            f"automatically.{_sibling_hint(task_id, repo_root)} Confirm the "
            "correct commit range manually and re-run."
        )
    commits = filtered

    newest = commits[0]
    oldest = commits[-1]
    fallback = f"{oldest}^..{newest}"

    fallback_result = _git(["diff", fallback], repo_root)
    diff_out = fallback_result.stdout or ""
    diff_lines = diff_out.count("\n")
    if diff_lines == 0:
        raise SystemExit(
            f"No changes found compared to the base branch for TASK-{task_id} "
            f"in '{repo_root}'.{_sibling_hint(task_id, repo_root)}"
        )

    return {
        "range": fallback,
        "resolved_repo_root": repo_root,
        "diff_lines": diff_lines,
        "diff_lines_meaningful": _count_meaningful_lines(diff_out),
        "summary": diff_out[:SUMMARY_CHARS],
        "recovered_from_task_commits": True,
    }


def main(argv: list) -> int:
    if len(argv) < 3:
        print("Usage: tusk review-diff-range <task_id>", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use

    parser = argparse.ArgumentParser(
        prog="tusk review-diff-range",
        description="Compute the git diff range for /review-commits against a task's branch",
    )
    parser.add_argument("task_id", help="Task ID (integer or TASK-NNN prefix form)")
    args = parser.parse_args(argv[2:])

    task_id_raw = re.sub(r"^TASK-", "", args.task_id, flags=re.IGNORECASE)
    try:
        task_id = int(task_id_raw)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    repo_root = resolve_repo_root(db_path)

    try:
        result = compute_range(task_id, repo_root, db_path)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return 1

    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk review-diff-range <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
