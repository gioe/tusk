#!/usr/bin/env python3
"""End-of-run task summary: identity, cost, duration, diff stats, and criteria counts.

Called by the tusk wrapper at the end of every /tusk run (Step 12 of skills/tusk/SKILL.md),
after tusk merge / tusk abandon and before handing off to /retro, so the user sees a
canonical "here's the task that just finished" block before retrospective findings.

Invocation:
    tusk task-summary <task_id> [--format json|markdown]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (unused — kept for dispatch consistency)
    sys.argv[3:] — task_id + optional flags

Output shape (JSON, default):
    {
        "task_id": N,
        "prefixed_id": "TASK-N",
        "summary": "...",
        "status": "Done",
        "closed_reason": "completed" | "wont_do" | "duplicate" | "expired" | null,
        "cost": {"total": 0.1234, "skill_run_count": N},
        "baseline_comparison": {
            "bucket": "M" | null,
            "median_cost": 0.0612 | null,
            "n": N,
            "ratio": 2.5 | null,
            "threshold": N,
            "status": "compared" | "pending" | "no_complexity" | "no_peers"
        },
        "tokens": {"tokens_in": N, "tokens_out": N, "request_count": N},
        "duration": {
            "wall_seconds": N | null,
            "active_seconds": N,
            "started_at": "..." | null,
            "closed_at": "..." | null,
            "session_count": N
        },
        "diff": {
            "commits": N,
            "files_changed": N,
            "lines_added": N,
            "lines_removed": N
        },
        "criteria": {
            "total": N,
            "manual": N,
            "automated": N,
            "skip_notes": N,
            "deferred": N,
            "deferred_details": [
                {"id": N, "criterion": "...", "deferred_reason": "..."},
                ...
            ]
        },
        "review_passes": N,
        "reopen_count": N
    }

With --format markdown, the same data is rendered as a user-facing block.

Diff stats are derived from `git log --grep="[TASK-<id>]"` — commits that don't
reference the task ID are excluded, preventing cross-task pollution on shared
branches. The query is also scoped with `--since=<tasks.started_at>` so commits
from an earlier incarnation of the same numeric ID (e.g., after a fresh DB init
where IDs reset) are excluded. If the task was abandoned (no commits), all diff
fields are 0.

Exit codes:
    0 — success
    1 — error (bad arguments, task not found, DB issue)
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402 — loads tusk-db-lib.py, tusk-json-lib.py, tusk-pricing-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_pricing_lib = tusk_loader.load("tusk-pricing-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
task_grep_arg = _git_helpers.task_grep_arg
commit_changed_files = _git_helpers.commit_changed_files
filter_commits_by_block_overlap = _git_helpers.filter_commits_by_block_overlap


def _resolve_task_id(raw: str) -> int:
    """Accept '5' or 'TASK-5' → 5. Raises ValueError on junk."""
    return int(re.sub(r"^TASK-", "", raw, flags=re.IGNORECASE))


def fetch_identity(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, summary, status, closed_reason, complexity, started_at, closed_at "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "summary": row["summary"],
        "status": row["status"],
        "closed_reason": row["closed_reason"],
        "complexity": row["complexity"],
        "started_at": row["started_at"],
        "closed_at": row["closed_at"],
    }


def _skill_run_task_match_sql(task_ref: str) -> str:
    """SQL predicate matching directly attributed and session-window skill runs."""
    return (
        f"(sr.task_id = {task_ref} "
        f"OR (sr.task_id IS NULL "
        f"    AND sr.ended_at IS NOT NULL "
        f"    AND sr.started_at IS NOT NULL "
        f"    AND EXISTS ("
        f"        SELECT 1 FROM task_sessions ts "
        f"        WHERE ts.task_id = {task_ref} "
        f"          AND ts.started_at <= sr.started_at "
        f"          AND (ts.ended_at IS NULL OR ts.ended_at >= sr.started_at)"
        f"    )))"
    )


def _has_cost(value) -> bool:
    return value is not None


def _has_unavailable_telemetry(row: sqlite3.Row) -> bool:
    return bool(
        row["ended_at"]
        and row["cost_dollars"] is None
        and row["telemetry_status"] in {
            "transcript_missing",
            "no_usage",
            "model_missing",
            "unpriced_model",
        }
    )


def _has_accounting(row: sqlite3.Row) -> bool:
    if row["telemetry_status"] in {"pending", "cancelled"}:
        return False
    return _has_cost(row["cost_dollars"]) or _has_unavailable_telemetry(row)


def _parse_cost_window(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _skill_run_contained_in_costed_session(sr: sqlite3.Row, sessions: list[sqlite3.Row]) -> bool:
    if not _has_accounting(sr):
        return False

    sr_start = _parse_cost_window(sr["started_at"])
    sr_end = _parse_cost_window(sr["ended_at"])
    if sr_start is None or sr_end is None:
        return False

    for session in sessions:
        if sr["task_id"] is not None and session["task_id"] != sr["task_id"]:
            continue
        if not _has_accounting(session):
            continue
        if _has_cost(sr["cost_dollars"]) and not _has_cost(session["cost_dollars"]):
            continue
        session_start = _parse_cost_window(session["started_at"])
        session_end = _parse_cost_window(session["ended_at"])
        if session_start is None or session_end is None:
            continue
        if session_start <= sr_start and sr_end <= session_end:
            return True
    return False


def _is_shadowed_tusk_skill_run(sr: sqlite3.Row, sessions: list[sqlite3.Row]) -> bool:
    """Return true when a tusk skill run is already represented by a session.

    ``/tusk`` opens both a task_session and a ``skill_runs.skill_name='tusk'``
    row. Session cost is authoritative for the main development run; the skill
    row is kept for skill-run drilldown and would double-count the same window.
    """
    if sr["skill_name"] != "tusk":
        return False
    if not _has_accounting(sr):
        return False

    sr_start = sr["started_at"] or ""
    for session in sessions:
        if sr["task_id"] is not None and session["task_id"] != sr["task_id"]:
            continue
        if not _has_accounting(session):
            continue
        if _has_cost(sr["cost_dollars"]) and not _has_cost(session["cost_dollars"]):
            continue
        session_start = session["started_at"] or ""
        if sr_start[:16] == session_start[:16]:
            return True
    return False


def _fetch_task_sessions_for_cost(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT id, task_id, started_at, ended_at, cost_dollars, telemetry_status "
            "FROM task_sessions WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "telemetry_status" in str(exc):
            return conn.execute(
                "SELECT id, task_id, started_at, ended_at, cost_dollars, "
                "NULL AS telemetry_status FROM task_sessions WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        if "cost_dollars" not in str(exc):
            raise
        return conn.execute(
            "SELECT id, task_id, started_at, ended_at, NULL AS cost_dollars, "
            "NULL AS telemetry_status "
            "FROM task_sessions WHERE task_id = ?",
            (task_id,),
        ).fetchall()


def _fetch_skill_runs_for_cost(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT id, skill_name, task_id, started_at, ended_at, cost_dollars, telemetry_status "
            "FROM skill_runs sr "
            f"WHERE {_skill_run_task_match_sql('?')}",
            (task_id, task_id),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "telemetry_status" in str(exc):
            return conn.execute(
                "SELECT id, skill_name, task_id, started_at, ended_at, cost_dollars, "
                "NULL AS telemetry_status FROM skill_runs sr "
                f"WHERE {_skill_run_task_match_sql('?')}",
                (task_id, task_id),
            ).fetchall()
        if "skill_name" not in str(exc):
            raise
        return conn.execute(
            "SELECT id, NULL AS skill_name, task_id, started_at, ended_at, cost_dollars, "
            "NULL AS telemetry_status "
            "FROM skill_runs sr "
            f"WHERE {_skill_run_task_match_sql('?')}",
            (task_id, task_id),
        ).fetchall()


def fetch_cost(conn: sqlite3.Connection, task_id: int) -> dict:
    """All-in cost for the task's main session and extra skill runs.

    This mirrors ``tusk cost`` accounting at task scope: sum costed
    task_sessions plus non-shadow skill_runs. Modern skill_runs carry task_id
    directly; older or manually-started rows may lack that attribution, so
    include completed un-attributed runs that started inside one of the task's
    recorded sessions. The ``skill_run_count`` field is the number of costed
    run windows included in the total.
    """
    sessions = _fetch_task_sessions_for_cost(conn, task_id)
    skill_runs = _fetch_skill_runs_for_cost(conn, task_id)

    total = 0.0
    count = 0
    unavailable_count = 0

    for session in sessions:
        if _has_unavailable_telemetry(session):
            count += 1
            unavailable_count += 1
            continue
        if not _has_accounting(session):
            continue
        total += float(session["cost_dollars"])
        count += 1

    for sr in skill_runs:
        if not _has_accounting(sr):
            continue
        if _is_shadowed_tusk_skill_run(sr, sessions):
            continue
        if _skill_run_contained_in_costed_session(sr, sessions):
            continue
        count += 1
        if _has_unavailable_telemetry(sr):
            unavailable_count += 1
        else:
            total += float(sr["cost_dollars"])

    result = {
        "total": round(total, 4),
        "skill_run_count": count,
    }
    if unavailable_count:
        result["unavailable_count"] = unavailable_count
    return result


def fetch_baseline_comparison(
    conn: sqlite3.Connection,
    task_id: int,
    complexity: str | None,
    current_cost: float,
    threshold: int,
) -> dict:
    """Median cost of completed peers in the same complexity bucket.

    Median is robust to outlier sessions (a single runaway agent run won't skew
    the baseline). Peers are restricted to status='Done' AND closed_reason='completed'
    so wont_do/duplicate stubs don't dilute the bucket. Per-task peer cost uses
    fetch_cost's session plus non-shadow skill-run accounting, and tasks with
    zero recorded cost are excluded so empty/orphaned rows don't drag the median
    to zero.

    Status values:
        no_complexity — current task has no complexity assigned (cannot bucket)
        no_peers      — bucket has zero qualifying peers (first task in bucket)
        pending       — bucket has 1..threshold-1 peers (sample too small to compare)
        compared      — bucket has >= threshold peers; ratio is populated
    """
    if not complexity:
        return {
            "bucket": None,
            "median_cost": None,
            "n": 0,
            "ratio": None,
            "threshold": threshold,
            "status": "no_complexity",
        }

    rows = conn.execute(
        "SELECT id "
        "FROM tasks t "
        "WHERE t.status = 'Done' "
        "  AND t.closed_reason = 'completed' "
        "  AND t.complexity = ? "
        "  AND t.id <> ?",
        (complexity, task_id),
    ).fetchall()

    peer_costs = sorted(
        cost["total"]
        for row in rows
        if (cost := fetch_cost(conn, int(row["id"])))["total"] > 0
    )
    n = len(peer_costs)

    if n == 0:
        return {
            "bucket": complexity,
            "median_cost": None,
            "n": 0,
            "ratio": None,
            "threshold": threshold,
            "status": "no_peers",
        }

    if n % 2 == 1:
        median = peer_costs[n // 2]
    else:
        median = (peer_costs[n // 2 - 1] + peer_costs[n // 2]) / 2

    if n < threshold:
        return {
            "bucket": complexity,
            "median_cost": round(median, 4),
            "n": n,
            "ratio": None,
            "threshold": threshold,
            "status": "pending",
        }

    # Suppress the multiplier for in-progress / not-yet-started tasks: a zero
    # current_cost would otherwise render as "0.0x baseline", which reads as
    # "this task was cheap" rather than "this task hasn't accumulated cost yet".
    # The bucket median + n still ship in compared status — useful context even
    # before the run finishes.
    ratio = current_cost / median if (median > 0 and current_cost > 0) else None
    return {
        "bucket": complexity,
        "median_cost": round(median, 4),
        "n": n,
        "ratio": round(ratio, 2) if ratio is not None else None,
        "threshold": threshold,
        "status": "compared",
    }


def fetch_tokens(conn: sqlite3.Connection, task_id: int) -> dict:
    """Sum tokens_in, tokens_out, request_count across skill_runs for the task."""
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) AS tin, "
        "       COALESCE(SUM(tokens_out), 0) AS tout, "
        "       COALESCE(SUM(request_count), 0) AS req "
        "FROM skill_runs sr "
        f"WHERE {_skill_run_task_match_sql('?')}",
        (task_id, task_id),
    ).fetchone()
    return {
        "tokens_in": int(row["tin"] or 0),
        "tokens_out": int(row["tout"] or 0),
        "request_count": int(row["req"] or 0),
    }


def fetch_duration(conn: sqlite3.Connection, task_id: int, identity: dict) -> dict:
    """Wall time = earliest session start → task closed_at; active = SUM of
    per-session idle-gap-discounted active_seconds (issue #1069), falling
    back per-row to duration_seconds for legacy rows closed before schema 79
    computed the transcript-derived value."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, "
            "       MIN(started_at) AS first_start, "
            "       COALESCE(SUM(COALESCE(active_seconds, duration_seconds)), 0) AS active "
            "FROM task_sessions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Pre-migration schema without active_seconds — legacy behavior.
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, "
            "       MIN(started_at) AS first_start, "
            "       COALESCE(SUM(duration_seconds), 0) AS active "
            "FROM task_sessions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    first_start = row["first_start"]
    closed_at = identity["closed_at"]
    wall = None
    if first_start and closed_at:
        try:
            start_dt = _pricing_lib.parse_sqlite_timestamp(first_start)
            end_dt = _pricing_lib.parse_sqlite_timestamp(closed_at)
            wall = int((end_dt - start_dt).total_seconds())
            if wall < 0:
                wall = 0
        except (ValueError, TypeError):
            wall = None
    return {
        "wall_seconds": wall,
        "active_seconds": int(row["active"] or 0),
        "started_at": first_start,
        "closed_at": closed_at,
        "session_count": int(row["cnt"] or 0),
    }


def _parse_numstat_blocks(stdout: str) -> tuple[dict, dict]:
    """Parse git numstat output emitted with ``--format=__COMMIT__ %H %P``."""
    commit_files: dict[str, list[tuple[str, str, str]]] = {}
    commit_parents: dict[str, list[str]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("__COMMIT__ "):
            tokens = line.split(" ", 1)[1].strip().split()
            if not tokens:
                current = None
                continue
            current = tokens[0]
            commit_files.setdefault(current, [])
            commit_parents[current] = tokens[1:]
            continue
        if current is None:
            continue
        # numstat row: "<added>\t<removed>\t<path>" (or "- -" for binary files)
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, r, path = parts[0], parts[1], parts[2]
        commit_files[current].append((a, r, path))
    return commit_files, commit_parents


def _completed_criterion_commit_hashes(
    task_id: int, conn: sqlite3.Connection | None
) -> list[str]:
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT commit_hash FROM acceptance_criteria "
        "WHERE task_id = ? AND is_completed = 1 "
        "  AND commit_hash IS NOT NULL AND TRIM(commit_hash) <> '' "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    return [r["commit_hash"] for r in rows]


def _task_chain_from_criterion_hash(
    task_id: int,
    repo_root: str,
    sha: str,
    since: str | None = None,
) -> list[str]:
    """Return the contiguous first-parent [TASK-N] chain ending at ``sha``.

    A completed criterion may point only at a later skip-verify commit on a
    task branch. If that branch is no longer referenced, ``git show <sha>``
    undercounts earlier manual [TASK-N] commits and prevents the fsck fallback
    from running. Walking first-parent ancestry from the recorded tip recovers
    the whole task-local commit chain while stopping at the first non-task
    parent so unrelated base history is not included.
    """
    cmd = ["git", "log", "--first-parent", "--format=%H%x00%s", sha]
    if since:
        cmd.append(f"--since={since} UTC")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    needle = f"[TASK-{task_id}]"
    chain: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        commit_sha, _sep, subject = line.partition("\x00")
        if needle in subject:
            chain.append(commit_sha)
            continue
        if chain:
            break
    return chain


def _criterion_hash_numstats(
    task_id: int,
    repo_root: str,
    conn: sqlite3.Connection,
    since: str | None = None,
) -> tuple[dict, dict]:
    """Recover numstat blocks from completed criteria commit hashes.

    This is a fallback for rebase/no-checkout merge paths where the rewritten
    task commit exists locally by SHA but is not visible to the summarizing
    checkout's ``git log --all`` ref scan. Missing stale hashes are skipped.
    When a criterion hash points at the tip of an unreferenced task branch,
    recover contiguous first-parent [TASK-N] ancestors as part of the same
    task-local chain instead of reporting only the tip commit (issue #917).
    """
    commit_files: dict[str, list[tuple[str, str, str]]] = {}
    commit_parents: dict[str, list[str]] = {}
    for sha in _completed_criterion_commit_hashes(task_id, conn):
        shas = _task_chain_from_criterion_hash(task_id, repo_root, sha, since=since)
        if not shas:
            shas = [sha]
        try:
            result = subprocess.run(
                [
                    "git", "show",
                    "--numstat",
                    "--format=__COMMIT__ %H %P",
                    *shas,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=repo_root,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        files, parents = _parse_numstat_blocks(result.stdout)
        commit_files.update(files)
        commit_parents.update(parents)
    return commit_files, commit_parents


def _summarize_commit_files(commit_files: dict) -> dict:
    files: set[str] = set()
    added = 0
    removed = 0
    for rows in commit_files.values():
        for a, r, path in rows:
            files.add(path)
            try:
                added += int(a)
            except ValueError:
                pass  # binary: "-"
            try:
                removed += int(r)
            except ValueError:
                pass
    return {
        "commits": len(commit_files),
        "files_changed": len(files),
        "lines_added": added,
        "lines_removed": removed,
    }


def _emit_scope_enforced_bypass(task_id: int) -> None:
    """One-line stderr note when the scope_enforced=1 bypass fires.

    TASK-472: when ``tasks.scope_enforced=1``, the commit-time scope guard
    has already filtered out-of-scope writes — the block-level prefix-
    collision filter is unnecessary, so every [TASK-<id>] commit is kept.
    This note records that the bypass path was hit so an operator can
    verify the new flow is hot.

    TTY-gated identically to ``_emit_recovery_tier_diagnostic`` (issue #850):
    silent when stderr is not a TTY (agent transcripts, piped logs, CI runs),
    silenced unconditionally by ``TUSK_QUIET=1``, force-emitted in non-TTY
    contexts by ``TUSK_FORCE_WARN=1`` (used by the regression tests).
    """
    if os.environ.get("TUSK_QUIET"):
        return
    if not os.environ.get("TUSK_FORCE_WARN") and not sys.stderr.isatty():
        return
    print(
        f"tusk: note — task-summary bypassed block-level scope filter for TASK-{task_id} "
        f"(scope_enforced=1; commits are authoritative). "
        f"(TUSK_QUIET=1 to silence)",
        file=sys.stderr,
    )


def _emit_recovery_tier_diagnostic(tier_name: str) -> None:
    """Print a single-line stderr note naming the recovery tier that produced commits.

    Gated identically to ``bin/tusk``'s ``maybe_warn_cross_repo_drift`` /
    ``maybe_warn_source_repo_stale`` precedent (issue #850): silent when
    stderr is not a TTY (agent callers, piped/captured stderr, CI logs),
    silenced unconditionally by ``TUSK_QUIET=1``, force-emitted in non-TTY
    contexts by ``TUSK_FORCE_WARN=1``.

    The TTY gate keeps agent transcripts and CI logs clean — these are
    downstream-consumed by LLMs and humans who can't act on the note; the
    diagnostic only helps an operator watching the terminal of a one-off
    ``tusk task-summary`` invocation.
    """
    if os.environ.get("TUSK_QUIET"):
        return
    if not os.environ.get("TUSK_FORCE_WARN") and not sys.stderr.isatty():
        return
    print(
        f"tusk: note — task-summary recovered diff via {tier_name} "
        f"(refresh-fetch / criterion-hash / fsck-unreachable). "
        f"(TUSK_QUIET=1 to silence)",
        file=sys.stderr,
    )


def _try_fetch_default_branch(repo_root: str) -> None:
    """Thin wrapper around ``tusk-git-helpers.try_fetch_default_branch``.

    Kept as a module-level function (not an alias) so existing monkeypatch
    points in ``tests/integration/test_task_summary_nocheckout_recovery.py``
    continue to work — those tests do ``monkeypatch.setattr(mod,
    "_try_fetch_default_branch", _spy)`` and the wrapper preserves the
    module-attribute lookup that makes the rebind effective from within
    ``fetch_diff`` (issue #848: the actual fetch logic is now shared with
    ``tusk task-done`` via ``find_task_commits_with_recovery``).
    """
    _git_helpers.try_fetch_default_branch(repo_root)


def _unreachable_task_commits(task_id: int, repo_root: str) -> tuple[dict, dict]:
    """Last-resort recovery: find [TASK-<id>] commits unreachable from any ref.

    Catches the post-no-checkout-push state where every other recovery path
    has failed (issue #845):
      * ``refs/remotes/origin/<default>`` was never advanced (or was deleted)
      * the best-effort ``git fetch`` retry above failed silently (broken
        remote URL, no network, no remote configured)
      * the criterion-hash fallback turned up nothing — criteria were closed
        via ``tusk task-done --force`` (so commit_hash is NULL), or the
        recorded commit_hash points to a pre-rebase SHA that's been GC'd

    SHA discovery is delegated to ``find_unreachable_task_commits`` in
    ``tusk-git-helpers.py`` (issue #848) so ``tusk task-done``'s auto-mark
    can share the same fsck logic; the numstat fetch below stays here
    because it is only meaningful for the diff-stats summary.

    Gated on the prior fallbacks producing nothing — ``git fsck`` walks the
    full object store and is O(objects), so paying this cost on the common
    path would penalize every well-merged task. Silent on every error:
    fsck failures, no candidates, and grep failures all return empty so
    fetch_diff continues with zeros rather than aborting the summary.
    """
    matching = _git_helpers.find_unreachable_task_commits(task_id, repo_root)
    if not matching:
        return {}, {}

    commit_files: dict[str, list[tuple[str, str, str]]] = {}
    commit_parents: dict[str, list[str]] = {}
    for sha in matching:
        try:
            show_res = subprocess.run(
                ["git", "show", "--numstat", "--format=__COMMIT__ %H %P", sha],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=repo_root,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if show_res.returncode != 0:
            continue
        files, parents = _parse_numstat_blocks(show_res.stdout)
        commit_files.update(files)
        commit_parents.update(parents)
    return commit_files, commit_parents


def _fetch_diff_from_stamped_sha(
    merge_commit_sha: str, repo_root: str,
    merge_base_sha: str | None = None,
) -> dict | None:
    """Summarize the merge's numstat output from stamped SHAs.

    Returns the same shape as ``_summarize_commit_files`` plus
    ``recovered_via="stamped-sha"`` on success, or None when the git
    invocation fails (missing object, corrupt repo). On None, the caller
    falls through to the existing scan + recovery chain so the
    stamped-but-unreachable case still produces stats. Issue #849.

    Two modes, gated by whether ``merge_base_sha`` was stamped:

    - **Range mode** (``merge_base_sha`` set and distinct from
      ``merge_commit_sha``, migration 72, TASK-452): runs
      ``git log --first-parent --numstat <base>..<tip>`` so a multi-commit
      fast-forward or no-checkout fast-forward push reports cumulative
      stats across every task commit on the branch — not just the tip
      that ``tusk merge`` stamped as the "merge commit". Without this,
      ``tusk task-summary`` understated multi-commit ff merges as 1
      commit / last-commit numstat (TASK-451's own closeout flagged the
      regression against TASK-454).

    - **Single-SHA mode** (``merge_base_sha`` None, or equal to
      ``merge_commit_sha``): runs ``git show --first-parent --numstat
      <merge_commit_sha>`` — correct for PR squash merges (one commit
      holds all task work) and for legacy pre-migration-72 rows where
      only the tip was stamped. ``--first-parent`` ensures squash merges
      don't double-count via the second parent.
    """
    if merge_base_sha and merge_base_sha != merge_commit_sha:
        cmd = [
            "git", "log", "--first-parent", "--numstat",
            "--format=__COMMIT__ %H %P",
            f"{merge_base_sha}..{merge_commit_sha}",
        ]
    else:
        cmd = [
            "git", "show", "--first-parent", "--numstat",
            "--format=__COMMIT__ %H %P", merge_commit_sha,
        ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    commit_files, _ = _parse_numstat_blocks(result.stdout)
    if not commit_files:
        return None
    summary = _summarize_commit_files(commit_files)
    summary["recovered_via"] = "stamped-sha"
    return summary


def _task_has_reopen_transition(
    conn: sqlite3.Connection, task_id: int,
) -> bool:
    """Return whether task history includes a transition back to To Do.

    Re-merging a reopened task overwrites ``tasks.merge_base_sha`` and
    ``merge_commit_sha`` with the latest lifecycle's range.  That stamp is
    therefore not cumulative.  Databases predating migration 53 have no
    transition table (and no recoverable reopen history), so they retain the
    existing stamped-SHA behavior.
    """
    try:
        row = conn.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM task_status_transitions "
            "WHERE task_id = ? AND to_status = 'To Do'"
            ")",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row[0]) if row is not None else False


def fetch_diff(
    task_id: int,
    repo_root: str,
    since: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Parse `git log --grep` output to collect commit count, unique files, and line deltas.

    `--all` scans every ref so post-merge commits (now on the default branch)
    are still found. The `[TASK-<id>]` grep filter excludes commits for other
    tasks that happen to sit on the same branch history. When `since` is
    provided (typically `tasks.started_at`), `--since=<since> UTC` is appended
    so commits authored before this task's lifetime — e.g. an earlier
    incarnation of the same numeric ID after a fresh DB init — are excluded.
    The "UTC" suffix anchors the SQLite-stored UTC timestamp against git's
    local-time interpretation of `--since`.

    When ``conn`` is provided and the task has a positive scope signal
    (referenced paths in summary/description/criteria/specs), the
    prefix-collision file-overlap heuristic (issue #656) drops candidate
    commits whose file diff doesn't intersect those paths — so a stray
    ``[TASK-<id>]`` commit (recycled task ID, fat-fingered message authored
    after this task started) doesn't inflate the diff stats surfaced in the
    end-of-run summary.

    The filter is applied **block-level**, not per-commit (issue #663): all
    grep-matched commits are grouped into connected components by their
    parent chain, then a block survives if *any* commit in the block touches
    a referenced path. This preserves legitimate sibling commits whose
    individual diffs don't name task paths — VERSION bumps, CHANGELOG edits,
    new test fixtures, and net-new files that by definition can't be
    pre-named in the task description. Genuine prefix collisions (commits
    landed in a different session, on a different branch, or simply not
    contiguous with the legitimate work) form their own block; if none of
    that block's files touch the scope signal, the whole block drops out.

    The other callers of ``task_referenced_paths`` (``tusk check-deliverables``
    and ``tusk task-unstart``) intentionally retain per-commit semantics
    because their question is "is *this specific commit* a prefix collision
    we should ignore?", not "is this commit part of the cluster of work for
    this task?". Skipped here when ``conn`` is None or no scope signal exists.

    **No-checkout fast-forward recovery** (issues #757/#797/#812/#816/#820/#822/#827):
    when the initial ``--all`` scan returns empty, a best-effort
    ``git fetch origin <default>`` is performed and the scan is retried. This
    catches the post-no-checkout-push state where ``refs/remotes/origin/<default>``
    was somehow not advanced by the push (some git configs/network conditions
    leave the local remote-tracking ref behind even after a successful push).
    The fetch is gated to the empty-scan case, so the common-path overhead is
    zero. The existing ``_criterion_hash_numstats`` fallback still fires after
    the retry if both attempts come up empty.

    **Unreachable-object recovery** (issue #845): when the fetch retry AND
    the criterion-hash fallback both come up empty, ``_unreachable_task_commits``
    enumerates unreachable commits in the local object store via
    ``git fsck --unreachable --no-reflogs`` and filters by the [TASK-<id>]
    grep. This catches the manual ``tusk task-done --reason completed``
    closeout path when (a) the local remote-tracking ref is stale, (b) the
    fetch retry fails silently because the remote is unreachable, and
    (c) the criteria were closed without a commit_hash (``--force`` close, or
    a stale pre-rebase SHA that was GC'd). The commit object is still in the
    shared ``.git/objects`` directory because no-checkout pushes deposit it
    before ``git worktree remove`` tears the sibling worktree down — fsck is
    the only local-only mechanism that finds it.
    """
    zero = {
        "commits": 0,
        "files_changed": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "recovered_via": None,
    }

    # Fast-path: when ``tusk merge`` stamped tasks.merge_commit_sha at close
    # time (migration 70, issue #849), short-circuit the ref/grep scan and
    # the 3-tier recovery chain entirely. Migration 72 (TASK-452) added a
    # companion ``merge_base_sha`` so the ff and no-checkout paths can run
    # ``git log <base>..<tip>`` for cumulative multi-commit stats instead
    # of ``git show <tip>`` (which only sees the last commit). PR squash
    # rows continue to stamp base NULL and use the single-SHA path —
    # one squash commit holds all task work, so ``git show`` is correct.
    # Legacy pre-migration-70 tasks carry both NULLs and fall through to
    # the existing scan + recovery chain unchanged.
    if conn is not None and not _task_has_reopen_transition(conn, task_id):
        try:
            row = conn.execute(
                "SELECT merge_commit_sha, merge_base_sha FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Pre-migration-72 DB: merge_base_sha column absent. Retry with
            # the v70 column shape so the fast-path stays available; the
            # absent base means single-SHA mode, identical to today.
            try:
                row = conn.execute(
                    "SELECT merge_commit_sha FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None  # column absent on pre-migration-70 DBs
        if row is not None:
            keys = row.keys() if hasattr(row, "keys") else ()
            stamped_sha = row["merge_commit_sha"] if "merge_commit_sha" in keys else row[0]
            base_sha = row["merge_base_sha"] if "merge_base_sha" in keys else None
            if stamped_sha:
                fast_path = _fetch_diff_from_stamped_sha(
                    stamped_sha, repo_root, merge_base_sha=base_sha,
                )
                if fast_path is not None:
                    return fast_path

    cmd = [
        "git", "log", "--all",
        task_grep_arg(task_id),
        "--numstat",
        # %P expands to space-separated parent SHAs (zero, one, or many for
        # merge commits). The __COMMIT__ prefix unambiguously marks header
        # lines; numstat rows are tab-delimited "<added>\t<removed>\t<path>",
        # so a header with parent SHAs cannot collide with numstat shape.
        "--format=__COMMIT__ %H %P",
    ]
    if since:
        cmd.append(f"--since={since} UTC")

    def _run_scan() -> str | None:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=repo_root,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    stdout = _run_scan()
    if stdout is None:
        return zero

    # Bucket numstat rows by commit; track parents so we can group commits
    # into topological blocks before applying the file-overlap filter.
    commit_files, commit_parents = _parse_numstat_blocks(stdout)

    # Track which recovery tier produced the final non-empty result so a single
    # TTY-gated diagnostic can be emitted below (issue #850). The cheap path
    # leaves this None and never emits.
    recovered_via: str | None = None

    # No-checkout fast-forward recovery: when the initial scan is empty, refresh
    # refs/remotes/origin/<default> and retry. Cheap when there's nothing to
    # catch (no commits to summarize anyway); high value when the remote-tracking
    # ref was left stale by the push.
    if not commit_files:
        _try_fetch_default_branch(repo_root)
        stdout = _run_scan()
        if stdout is not None:
            commit_files, commit_parents = _parse_numstat_blocks(stdout)
            if commit_files:
                recovered_via = "refresh-fetch"

    if conn is not None and not commit_files:
        commit_files, commit_parents = _criterion_hash_numstats(
            task_id, repo_root, conn, since=since,
        )
        if commit_files:
            recovered_via = "criterion-hash"

    # Unreachable-object recovery (issue #845): last-resort scan when every
    # ref-based and criterion-hash lookup has come up empty. Gated tightly so
    # the fsck cost only lands on the pathological path documented in
    # _unreachable_task_commits.
    if not commit_files:
        commit_files, commit_parents = _unreachable_task_commits(task_id, repo_root)
        if commit_files:
            recovered_via = "fsck-unreachable"

    if recovered_via is not None:
        _emit_recovery_tier_diagnostic(recovered_via)

    # TASK-472: when scope_enforced=1, the commit-time scope guard ensured
    # every [TASK-N] commit only touched authorized paths — prefix-collision
    # filtering is unnecessary, so all matched commits are kept verbatim. Read
    # the flag inside the same conn handle; legacy DBs without the column
    # behave as scope_enforced=0 and fall through to the existing block filter.
    scope_enforced = False
    if conn is not None and commit_files:
        try:
            row = conn.execute(
                "SELECT scope_enforced FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is not None:
                keys = row.keys() if hasattr(row, "keys") else ()
                val = row["scope_enforced"] if "scope_enforced" in keys else row[0]
                scope_enforced = bool(val)
        except sqlite3.OperationalError:
            scope_enforced = False
    if scope_enforced:
        _emit_scope_enforced_bypass(task_id)

    # Apply prefix-collision file-overlap heuristic (issue #656) at the
    # block level (issue #663) via the centralized helper (issue #855).
    # A "block" is a connected component on the parent graph restricted to
    # grep-matched commits — i.e. a contiguous run of [TASK-N] commits in
    # git history. The block survives if any of its commits touch a
    # referenced path (full path or basename, issue #670). Bare-basename
    # tokens like ``FULL-RETRO.md`` are resolved inside the helper so they
    # pull in commits touching e.g. skills/retro/FULL-RETRO.md. We pre-build
    # a path-set view of ``commit_files`` (numstat tuples → paths) and pass
    # the pre-fetched ``commit_parents`` so the helper does not re-issue
    # the git log subprocess for parent resolution.
    if not scope_enforced and conn is not None and commit_files:
        commits = list(commit_files.keys())
        cf_paths = {
            sha: {path for (_a, _r, path) in rows}
            for sha, rows in commit_files.items()
        }
        kept = filter_commits_by_block_overlap(
            commits, task_id, repo_root, conn,
            commit_files=cf_paths,
            commit_parents=commit_parents,
        )
        kept_set = set(kept)
        if kept_set != set(commits):
            commit_files = {
                sha: rows for sha, rows in commit_files.items()
                if sha in kept_set
            }

    result = _summarize_commit_files(commit_files)
    # Surface the recovery tier to JSON consumers (issue #852). The stderr
    # diagnostic above is TTY-gated and invisible to agent callers that capture
    # stderr; this field is the machine-readable equivalent so /tusk Step 12
    # and /address-issue Step 10 can answer "why are my stats zero" from the
    # JSON output alone. None on the cheap path (initial scan succeeded).
    result["recovered_via"] = recovered_via
    return result


def fetch_criteria(conn: sqlite3.Connection, task_id: int) -> dict:
    """Counts by kind and skip signal, plus per-criterion deferred details.

    `skip_notes` captures criteria closed with `--skip-verify --note "..."` (the
    note lands in `acceptance_criteria.skip_note`). `deferred` captures the
    `tusk criteria skip --reason` path which sets `is_deferred=1`. Together they
    cover every "acknowledged gap at close" signal the schema records.

    `deferred_details` is a per-row list (id, criterion, deferred_reason) so the
    markdown rollup and downstream consumers can distinguish *why* each
    criterion was deferred — chain orchestration vs not-applicable vs other
    rationales — instead of seeing only an aggregate count.
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN criterion_type = 'manual' THEN 1 ELSE 0 END) AS manual, "
        "  SUM(CASE WHEN criterion_type IN ('code', 'test', 'file') THEN 1 ELSE 0 END) AS automated, "
        "  SUM(CASE WHEN skip_note IS NOT NULL AND TRIM(skip_note) <> '' THEN 1 ELSE 0 END) AS skip_notes, "
        "  SUM(CASE WHEN is_deferred = 1 THEN 1 ELSE 0 END) AS deferred "
        "FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    deferred_rows = conn.execute(
        "SELECT id, criterion, deferred_reason FROM acceptance_criteria "
        "WHERE task_id = ? AND is_deferred = 1 ORDER BY id",
        (task_id,),
    ).fetchall()
    return {
        "total": int(row["total"] or 0),
        "manual": int(row["manual"] or 0),
        "automated": int(row["automated"] or 0),
        "skip_notes": int(row["skip_notes"] or 0),
        "deferred": int(row["deferred"] or 0),
        "deferred_details": [
            {
                "id": int(r["id"]),
                "criterion": r["criterion"],
                "deferred_reason": r["deferred_reason"],
            }
            for r in deferred_rows
        ],
    }


def fetch_review_passes(conn: sqlite3.Connection, task_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM code_reviews WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["cnt"] or 0)


def fetch_reopen_count(conn: sqlite3.Connection, task_id: int) -> int:
    """Transitions back into 'To Do' — covers both mid-task rework and post-Done reopens."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_status_transitions "
        "WHERE task_id = ? AND to_status = 'To Do'",
        (task_id,),
    ).fetchone()
    return int(row["cnt"] or 0)


def build_summary(
    conn: sqlite3.Connection,
    task_id: int,
    repo_root: str,
    baseline_threshold: int = 10,
) -> dict | None:
    identity = fetch_identity(conn, task_id)
    if identity is None:
        return None
    cost = fetch_cost(conn, task_id)
    return {
        "task_id": identity["id"],
        "prefixed_id": f"TASK-{identity['id']}",
        "summary": identity["summary"],
        "status": identity["status"],
        "closed_reason": identity["closed_reason"],
        "cost": cost,
        "baseline_comparison": fetch_baseline_comparison(
            conn, task_id, identity["complexity"], cost["total"], baseline_threshold
        ),
        "tokens": fetch_tokens(conn, task_id),
        "duration": fetch_duration(conn, task_id, identity),
        "diff": fetch_diff(task_id, repo_root, since=identity["started_at"], conn=conn),
        "criteria": fetch_criteria(conn, task_id),
        "review_passes": fetch_review_passes(conn, task_id),
        "reopen_count": fetch_reopen_count(conn, task_id),
    }


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def _render_cost_line(cost: dict, baseline: dict) -> str:
    plural = "s" if cost["skill_run_count"] != 1 else ""
    unavailable = cost.get("unavailable_count", 0)
    if unavailable:
        if unavailable == cost["skill_run_count"]:
            return (
                f"- **Cost:** unavailable across {cost['skill_run_count']} "
                f"completed run window{plural}"
            )
        return (
            f"- **Cost:** ${cost['total']:.4f} known subtotal across "
            f"{cost['skill_run_count']} run window{plural}; {unavailable} unavailable"
        )
    base = (
        f"- **Cost:** ${cost['total']:.4f} across "
        f"{cost['skill_run_count']} skill run{plural}"
    )
    status = baseline.get("status")
    if status == "compared":
        bucket_info = (
            f"{baseline['bucket']} median: ${baseline['median_cost']:.4f}, "
            f"n={baseline['n']}"
        )
        if baseline.get("ratio") is None:
            # Zero-cost current task: show the bucket context but skip the multiplier
            return f"{base} ({bucket_info})"
        return f"{base} — {baseline['ratio']:.1f}x baseline ({bucket_info})"
    if status in ("pending", "no_peers"):
        return (
            f"{base} (baseline pending — {baseline['bucket']} bucket has "
            f"{baseline['n']}/{baseline['threshold']} closed tasks)"
        )
    return base


def render_markdown(data: dict) -> str:
    closed = data["closed_reason"] or "—"
    cost = data["cost"]
    baseline = data["baseline_comparison"]
    dur = data["duration"]
    diff = data["diff"]
    crit = data["criteria"]

    lines = [
        f"## {data['prefixed_id']} — {data['summary']} ({data['status']} / {closed})",
        "",
        _render_cost_line(cost, baseline),
        f"- **Duration:** {_format_duration(dur['wall_seconds'])} wall / "
        f"{_format_duration(dur['active_seconds'])} active "
        f"({dur['session_count']} session{'s' if dur['session_count'] != 1 else ''})",
        f"- **Changes:** {diff['files_changed']} file"
        f"{'s' if diff['files_changed'] != 1 else ''} · "
        f"+{diff['lines_added']} / −{diff['lines_removed']} lines · "
        f"{diff['commits']} commit{'s' if diff['commits'] != 1 else ''}",
        f"- **Criteria:** {crit['total']} total "
        f"({crit['manual']} manual, {crit['automated']} automated)"
        + (
            f" · {crit['skip_notes']} skip-verify"
            if crit["skip_notes"]
            else ""
        )
        + (f" · {crit['deferred']} deferred" if crit["deferred"] else ""),
    ]
    if diff.get("recovered_via"):
        lines.append(
            f"- **Note:** diff stats recovered via `{diff['recovered_via']}` tier "
            f"(initial scan empty; surfaced from fallback)"
        )
    for d in crit.get("deferred_details", []):
        reason = d.get("deferred_reason") or "no reason given"
        lines.append(f"  - _Deferred #{d['id']} ({reason}):_ {d['criterion']}")
    lines.append(
        f"- **Review passes:** {data['review_passes']}"
        + (f" · **Reopened:** {data['reopen_count']}×" if data["reopen_count"] else "")
    )
    return "\n".join(lines)


def _load_baseline_threshold(config_path: str) -> int:
    """Read baseline_min_sample_size from config; default to 10 if missing/invalid/unreadable."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        val = cfg.get("baseline_min_sample_size", 10)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return 10


def main(argv: list) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk task-summary",
        description="Emit an end-of-run summary for a task (identity, cost, duration, diff, criteria).",
    )
    parser.add_argument("task_id", help="Task ID (integer or TASK-NNN prefix form)")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv[2:])

    try:
        task_id = _resolve_task_id(args.task_id)
    except ValueError:
        print(f"Invalid task ID: {args.task_id}", file=sys.stderr)
        return 1

    # repo_root is two levels up from the DB: tusk/tasks.db → tusk/ → repo_root
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    threshold = _load_baseline_threshold(config_path)

    conn = get_connection(db_path)
    try:
        data = build_summary(conn, task_id, repo_root, baseline_threshold=threshold)
        if data is None:
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1
        if args.format == "markdown":
            print(render_markdown(data))
        else:
            print(dumps(data))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-summary <task_id> [--format json|markdown]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
