#!/usr/bin/env python3
"""Check for existing deliverables when a task has criteria completed but no commits.

Called by the tusk wrapper:
    tusk check-deliverables <task_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3] — task_id (integer or TASK-NNN prefix form)

Output JSON:
    {
        "commits_found": bool,
        "files_found": bool,
        "files": ["path/that/exists", ...],
        "default_branch_commits": ["sha1", ...],
        "default_branch_commit_files": ["path/changed/by/default/commits", ...],
        "missing_creates_paths": ["declared/creates/path/that/is/absent", ...],
        "recommendation": "commits_found" | "merged_not_closed" | "merged_not_closed_low_confidence" | "mark_done" | "manual_pending" | "criteria_complete_no_commits" | "implement_fresh"
    }

Recommendations:
    "commits_found"                       — commits referencing this task exist on a non-default branch — normal path
    "merged_not_closed"                   — commits already on the default branch and their diff overlaps with task scope (or there is no scope signal to compare) — skip implementation, go straight to finalize
    "merged_not_closed_low_confidence"    — commits exist on the default branch but their diff doesn't overlap with files referenced in the task or with files modified on any feature branch — likely a [TASK-N] prefix-match false positive — verify before acting
    "mark_done"                           — no commits, but deliverable files found on disk AND at least one non-deferred criterion is non-manual AND the verification-spec gate passes (issue #1068: at least one incomplete code/file/test spec passes, or no runnable spec exists — test-type included per issue #1103) AND no source='creates' scope paths are missing — mark criteria done and merge
    "manual_pending"                      — no commits, deliverable files found on disk, BUT every non-deferred criterion is criterion_type='manual' (issue #806) — the file-existence signal is noise for manual criteria (a referenced gitignored file may exist regardless of whether the human performed the external work). Do NOT auto-close; proceed with implementation manually.
    "criteria_complete_no_commits"        — every non-deferred acceptance criterion is marked is_completed=1 but there are no [TASK-N] commits anywhere and no deliverable files on disk — salvage / converged-work / speculative-mark signal — investigate before re-implementing
    "implement_fresh"                     — no commits and either (a) no deliverable files found, (b) files exist but every incomplete code/file/test verification spec still fails (issue #1068: the deliverable is an EDIT to an existing file, so file existence is noise — verifiable_spec_count/passing_spec_count in the output record the gate decision; test-type specs included per issue #1103), or (c) task_scope has source='creates' paths that are still absent — proceed with implementation

Exit codes:
    0 — success (always, even if no commits/files)
    1 — error (bad arguments, task not found, DB issue, etc.)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
find_task_commits = _git_helpers.find_task_commits
extract_paths = _git_helpers.extract_paths
default_branch_of = _git_helpers.default_branch
commit_changed_files = _git_helpers.commit_changed_files
task_referenced_paths = _git_helpers.task_referenced_paths


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
    db_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    db_common = _git_common_dir(db_repo_root)
    if not db_common:
        return db_repo_root

    candidate = _git_stdout(["rev-parse", "--show-toplevel"], cwd or os.getcwd())
    if not candidate:
        return db_repo_root
    candidate = os.path.abspath(candidate)
    candidate_common = _git_common_dir(candidate)
    if candidate_common and candidate_common == db_common:
        return candidate
    return db_repo_root


def check_commits(task_id: int, repo_root: str, since: str | None = None) -> bool:
    """Return True if any commits reference [TASK-<id>] on any branch."""
    return bool(find_task_commits(task_id, repo_root, ["--all"], since=since))


def check_default_branch_commits(
    task_id: int, repo_root: str, since: str | None = None
) -> list:
    """Return commit SHAs on the default branch that reference [TASK-<id>]."""
    return find_task_commits(task_id, repo_root, [default_branch_of(repo_root)], since=since)


def _feature_branch_commits(
    task_id: int, repo_root: str, default_branch: str, since: str | None = None
) -> list:
    """Return [TASK-<id>] commit SHAs reachable from any ref EXCEPT the default branch."""
    return find_task_commits(
        task_id, repo_root, ["--all", "--not", default_branch], since=since
    )


def find_existing_files(task_id: int, conn: sqlite3.Connection, repo_root: str) -> list:
    """Return paths referenced in task text / criteria specs that exist on disk."""
    found = []
    for p in task_referenced_paths(task_id, conn):
        abs_path = p if os.path.isabs(p) else os.path.join(repo_root, p)
        if os.path.exists(abs_path):
            found.append(p)
    return found


def missing_creates_paths(task_id: int, conn: sqlite3.Connection, repo_root: str) -> list:
    """Return source='creates' task_scope paths that are not present on disk.

    Legacy DBs or minimal test fixtures without task_scope return no signal.
    """
    try:
        rows = conn.execute(
            "SELECT pattern FROM task_scope "
            "WHERE task_id = ? AND source = 'creates' ORDER BY id",
            (task_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    missing = []
    for row in rows:
        pattern = row["pattern"] if isinstance(row, sqlite3.Row) else row[0]
        abs_path = pattern if os.path.isabs(pattern) else os.path.join(repo_root, pattern)
        if not os.path.exists(abs_path):
            missing.append(pattern)
    return missing


def all_active_criteria_complete(task_id: int, conn: sqlite3.Connection) -> bool:
    """True iff the task has at least one non-deferred criterion AND every non-deferred criterion is_completed=1.

    Deferred criteria (is_deferred=1) are excluded — they're intentionally skipped per the
    `tusk criteria skip` flow and don't count toward the salvage signal. A task with zero
    non-deferred criteria returns False (no salvage signal — vacuous truth is not informative).
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(CASE WHEN COALESCE(is_deferred, 0) = 0 THEN 1 END) AS active, "
        "  COALESCE(SUM(CASE WHEN COALESCE(is_deferred, 0) = 0 AND is_completed = 1 THEN 1 ELSE 0 END), 0) AS done "
        "FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    active, done = row[0], row[1]
    return active > 0 and active == done


def all_active_criteria_are_manual(task_id: int, conn: sqlite3.Connection) -> bool:
    """True iff the task has at least one non-deferred criterion AND every
    non-deferred criterion has criterion_type='manual'.

    Issue #806: when this holds and a referenced file exists on disk,
    the "file exists implies deliverable shipped" heuristic is unreliable
    — manual criteria don't leave file artifacts (e.g., OAuth secret
    rotations live in external dashboards). Callers should downgrade
    mark_done to manual_pending in that case so the task is not silently
    auto-closed. The criterion_type column is NULL on old rows that pre-
    date the column; COALESCE treats NULL as 'manual' so legacy data
    follows the safer manual_pending path rather than the auto-close path.
    """
    row = conn.execute(
        "SELECT "
        "  COUNT(CASE WHEN COALESCE(is_deferred, 0) = 0 THEN 1 END) AS active, "
        "  COALESCE(SUM(CASE WHEN COALESCE(is_deferred, 0) = 0 "
        "                    AND COALESCE(criterion_type, 'manual') = 'manual' "
        "                THEN 1 ELSE 0 END), 0) AS manual_count "
        "FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    active, manual_count = row[0], row[1]
    return active > 0 and active == manual_count


def _is_negative_spec(spec: str) -> bool:
    """Return whether *spec* asserts absence rather than positive delivery."""
    normalized = " ".join(spec.strip().split())
    return (
        normalized.startswith("!")
        or normalized.startswith("test ! ")
        or normalized.startswith("test -z ")
        or normalized.startswith("[ ! ")
        or normalized.startswith("[ -z ")
    )


def verifiable_spec_results(
    task_id: int, conn: sqlite3.Connection
) -> tuple[int, int, int, int, int, int]:
    """Return total, positive, and negative runnable/passing spec counts.

    Issue #1068: file existence is noise when the deliverable is an EDIT to a
    file that already exists — the referenced path is on disk before any work
    happens, yet the verification specs still fail. Mirrors task-start's
    _count_criteria_already_passing scan (issue #1051): incomplete, non-deferred
    criteria with non-empty specs, run via tusk-criteria's run_verification
    (timeout-bounded).

    Issue #1103: test-type criteria are now included alongside code/file.
    Excluding them produced a false mark_done for edit-to-existing-file tasks
    whose ONLY verification spec is a failing test-type reproducer (concrete
    incident: TASK-662) — verifiable_spec_count came back 0, so the gate took
    the no-runnable-spec branch and recommended mark_done purely on file
    existence. run_verification is already timeout-bounded, so running the
    test-type spec once for the gate decision is the same cost tusk criteria
    done pays at merge.

    Best-effort by design: any failure returns (0, 0), which callers
    treat as "no verifiable signal" and preserve the legacy mark_done path.
    """
    try:
        rows = conn.execute(
            "SELECT criterion_type, verification_spec FROM acceptance_criteria "
            "WHERE task_id = ? AND is_completed = 0 AND COALESCE(is_deferred, 0) = 0 "
            "AND criterion_type IN ('code', 'file', 'test') "
            "AND verification_spec IS NOT NULL AND verification_spec != ''",
            (task_id,),
        ).fetchall()
        if not rows:
            return (0, 0, 0, 0, 0, 0)
        run_verification = tusk_loader.load("tusk-criteria").run_verification
        results = [
            (row[1], run_verification(row[0], row[1])["passed"])
            for row in rows
        ]
        positive_results = [passed for spec, passed in results if not _is_negative_spec(spec)]
        negative_results = [passed for spec, passed in results if _is_negative_spec(spec)]
        return (
            len(results),
            sum(passed for _, passed in results),
            len(positive_results),
            sum(positive_results),
            len(negative_results),
            sum(negative_results),
        )
    except Exception:
        return (0, 0, 0, 0, 0, 0)


def _task_scope_enforced(conn: sqlite3.Connection, task_id: int) -> bool:
    """Return True iff ``tasks.scope_enforced=1`` for ``task_id``.

    Legacy DBs without the column (pre-migration-73) treat the task as
    unenforced (returns False), so the merged_not_closed_low_confidence
    heuristic continues to fire on those rows.
    """
    try:
        row = conn.execute(
            "SELECT scope_enforced FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if not row:
        return False
    if isinstance(row, sqlite3.Row) and "scope_enforced" in row.keys():
        return bool(row["scope_enforced"])
    return bool(row[0])


def _emit_scope_enforced_bypass(task_id: int) -> None:
    """One-line stderr note when the scope_enforced=1 bypass fires.

    TASK-472: when ``tasks.scope_enforced=1``, the commit-time scope guard
    ensured every [TASK-<id>] commit only touched authorized paths, so the
    merged_not_closed_low_confidence downgrade can't represent a real
    prefix-match false positive. The note records that the bypass fired
    so an operator can verify the new flow is hot.

    TTY-gated like ``maybe_warn_cross_repo_drift`` (issue #850): silent
    when stderr is not a TTY, silenced unconditionally by ``TUSK_QUIET=1``,
    force-emitted in non-TTY contexts by ``TUSK_FORCE_WARN=1``.
    """
    if os.environ.get("TUSK_QUIET"):
        return
    if not os.environ.get("TUSK_FORCE_WARN") and not sys.stderr.isatty():
        return
    print(
        f"tusk: note — check-deliverables bypassed scope-overlap downgrade for TASK-{task_id} "
        f"(scope_enforced=1; merged commits are authoritative). "
        f"(TUSK_QUIET=1 to silence)",
        file=sys.stderr,
    )


def _task_started_at(conn: sqlite3.Connection, task_id: int) -> str | None:
    try:
        row = conn.execute(
            "SELECT started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    if isinstance(row, sqlite3.Row) and "started_at" in row.keys():
        return row["started_at"]
    return row[0]


def main(argv: list) -> int:
    if len(argv) < 3:
        print("Usage: tusk check-deliverables <task_id>", file=sys.stderr)
        return 1

    db_path = argv[0]
    # argv[1] is config_path — reserved for future use
    task_id_raw = re.sub(r"^TASK-", "", argv[2], flags=re.IGNORECASE)
    try:
        task_id = int(task_id_raw)
    except ValueError:
        print(f"Invalid task ID: {argv[2]}", file=sys.stderr)
        return 1

    repo_root = resolve_repo_root(db_path)

    conn = get_connection(db_path)
    try:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            print(f"Task {task_id} not found", file=sys.stderr)
            return 1

        started_at = _task_started_at(conn, task_id)
        creates_paths_missing = missing_creates_paths(task_id, conn, repo_root)
        default_branch = default_branch_of(repo_root)
        default_commits = find_task_commits(
            task_id, repo_root, [default_branch], since=started_at
        )
        if default_commits:
            default_files = commit_changed_files(default_commits, repo_root)
            task_paths = set(task_referenced_paths(task_id, conn))
            feature_commits = _feature_branch_commits(
                task_id, repo_root, default_branch, since=started_at
            )
            feature_files = commit_changed_files(feature_commits, repo_root)
            scope = task_paths | feature_files
            # TASK-472: when scope_enforced=1 the commit-time guard ensured
            # every [TASK-N] commit on the default branch only touched
            # authorized paths — there is no prefix-match false positive
            # to downgrade. Trust the merged state and short-circuit.
            # Legacy tasks (scope_enforced=0) fall through to the
            # aggregate-level file-overlap heuristic below.
            if _task_scope_enforced(conn, task_id):
                _emit_scope_enforced_bypass(task_id)
                recommendation = "merged_not_closed"
            # Aggregate-level file-overlap (intentional, distinct from
            # tusk-task-summary.py's block-level variant — issue #663).
            # `default_files` is the union of ALL matched commits on the
            # default branch, so this asks "is the whole batch off-scope?"
            # — the right granularity for a binary "downgrade to
            # merged_not_closed_low_confidence vs. proceed" decision.
            # Downgrade only when we have a positive scope signal that
            # fails to overlap. Empty scope = no signal, not a downgrade
            # trigger — preserve existing behavior.
            elif scope and not (scope & default_files):
                recommendation = "merged_not_closed_low_confidence"
            else:
                recommendation = "merged_not_closed"
            output = {
                "commits_found": True,
                "files_found": False,
                "files": [],
                "default_branch_commits": default_commits,
                "default_branch_commit_files": sorted(default_files),
                "missing_creates_paths": creates_paths_missing,
                "recommendation": recommendation,
            }
        elif check_commits(task_id, repo_root, since=started_at):
            output = {
                "commits_found": True,
                "files_found": False,
                "files": [],
                "default_branch_commits": [],
                "default_branch_commit_files": [],
                "missing_creates_paths": creates_paths_missing,
                "recommendation": "commits_found",
            }
        else:
            files = find_existing_files(task_id, conn, repo_root)
            files_found = bool(files)
            verifiable_specs, passing_specs = (0, 0)
            positive_specs, passing_positive_specs = (0, 0)
            negative_specs, passing_negative_specs = (0, 0)
            if files_found:
                # Issue #806: when every non-deferred criterion is manual,
                # file existence is noise — manual criteria don't leave
                # file artifacts. Surface the file-existence signal via
                # manual_pending so callers do not silently auto-close.
                if all_active_criteria_are_manual(task_id, conn):
                    recommendation = "manual_pending"
                else:
                    # Issue #1068: file existence is also noise when the
                    # deliverable is an EDIT to an existing file — run the
                    # incomplete code/file verification specs (same scan as
                    # task-start's criteria_already_passing) and downgrade
                    # when every verifiable spec still fails. No verifiable
                    # specs at all keeps the legacy mark_done behavior.
                    (
                        verifiable_specs,
                        passing_specs,
                        positive_specs,
                        passing_positive_specs,
                        negative_specs,
                        passing_negative_specs,
                    ) = verifiable_spec_results(task_id, conn)
                    if creates_paths_missing:
                        recommendation = "implement_fresh"
                    elif verifiable_specs > 0 and (
                        positive_specs == 0
                        or passing_positive_specs < positive_specs
                    ):
                        recommendation = "implement_fresh"
                    else:
                        recommendation = "mark_done"
            elif all_active_criteria_complete(task_id, conn):
                recommendation = "criteria_complete_no_commits"
            else:
                recommendation = "implement_fresh"
            output = {
                "commits_found": False,
                "files_found": files_found,
                "files": files,
                "default_branch_commits": [],
                "default_branch_commit_files": [],
                "verifiable_spec_count": verifiable_specs,
                "passing_spec_count": passing_specs,
                "positive_spec_count": positive_specs,
                "passing_positive_spec_count": passing_positive_specs,
                "negative_spec_count": negative_specs,
                "passing_negative_spec_count": passing_negative_specs,
                "missing_creates_paths": creates_paths_missing,
                "recommendation": recommendation,
            }

        print(dumps(output))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk check-deliverables <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
