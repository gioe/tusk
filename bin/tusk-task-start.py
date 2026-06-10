#!/usr/bin/env python3
"""Consolidate task-start setup into a single CLI command.

Called by the tusk wrapper:
    tusk task-start [<task_id>] [--force] [--force-deps] [--force-contingent] [--force-not-before] [--force-session] [--agent <name>] [--skill <name>]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — [task_id] [--force] [--force-deps] [--force-contingent] [--force-not-before] [--force-session] [--agent <name>] [--skill <name>]

When task_id is omitted, the top WSJF-ranked ready task is picked from
v_ready_tasks (same ranking logic tusk-task-select uses) and started in a
single call — this eliminates the select+start round-trip the /tusk no-arg
path used to pay. If no ready tasks exist, exits 1 with the same stderr
message tusk-task-select historically emitted.

Performs all setup steps for beginning work on a task:
  1. Fetch the task (validate it exists and is actionable)
  2. Check for prior progress checkpoints
  3. Reuse an open session or create a new one
  4. Update task status to 'In Progress' (if not already)
  5. Fetch acceptance criteria
  6. Return a JSON blob with task details, progress, criteria, and session_id

--force: bypass the zero-criteria guard (emits a warning but proceeds)
--force-deps: bypass the unmet-`blocks`-dependency guard (emits a warning but proceeds)
--force-contingent: bypass the open contingent dependency guard (emits a warning but proceeds)
--force-not-before: bypass a future not_before timestamp (emits a warning but proceeds)
--force-session: reuse an existing active session from outside the task workspace
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-rank-lib.py, tusk-git-helpers.py, tusk-criteria.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_rank_lib = tusk_loader.load("tusk-rank-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
select_top_ready_task = _rank_lib.select_top_ready_task
empty_backlog_message = _rank_lib.empty_backlog_message


# Content-word overlap threshold below which a prior progress.next_steps is
# considered stale relative to the current task summary/description. Tuned so
# typo fixes, rewording, and normal in-progress notes stay quiet; full scope
# rewrites trip the warning. See tests in test_task_start_stale_progress.py.
_STALE_PROGRESS_OVERLAP_THRESHOLD = 0.15
_STALE_PROGRESS_MIN_TOKENS = 8

_STALE_PROGRESS_STOPWORDS = frozenset({
    "a", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been",
    "but", "by", "can", "could", "do", "does", "doing", "done", "for", "from",
    "had", "has", "have", "he", "her", "here", "his", "how", "if", "in", "into",
    "is", "it", "its", "just", "like", "may", "might", "most", "much", "no",
    "not", "now", "of", "on", "one", "only", "or", "our", "over", "same", "she",
    "so", "some", "still", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "to", "too", "two",
    "up", "us", "very", "was", "we", "were", "what", "when", "which", "while",
    "who", "why", "will", "with", "you", "your",
})

_DEFER_TRIGGER_RE = re.compile(
    r"^(?:defer\s+trigger\s*:|defer\s*:|defer\s+until\b|wait\s+for\b)",
    re.IGNORECASE,
)


def _stem_token(word: str) -> str:
    """Crude suffix stripping so "cache"/"caching" and "typo"/"typos" collide.

    Strips a plural/verb-form suffix when present, otherwise strips a bare
    trailing "e" (so "cache" matches "caching" → "cach"). Guarded on length 4
    to avoid over-stemming short tokens.
    """
    for suffix in ("ing", "ied", "ies", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    if word.endswith("e") and len(word) >= 5:
        return word[:-1]
    return word


def _extract_content_tokens(text: str) -> set[str]:
    """Return the set of stemmed, lowercased content tokens in `text`.

    Strips stopwords, short tokens, and punctuation. Intended for rough
    vocabulary-overlap comparison — not a real NLP stemmer.
    """
    if not text:
        return set()
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text.lower())
    return {
        _stem_token(tok)
        for tok in raw
        if len(tok) >= 3 and tok not in _STALE_PROGRESS_STOPWORDS
    }


def _progress_next_steps_is_stale(next_steps: str, current_text: str) -> bool:
    """True when `next_steps` shares too little vocabulary with `current_text`.

    Returns False when next_steps is too short to judge, or when current_text
    has no content tokens at all.
    """
    next_tokens = _extract_content_tokens(next_steps)
    if len(next_tokens) < _STALE_PROGRESS_MIN_TOKENS:
        return False
    current_tokens = _extract_content_tokens(current_text)
    if not current_tokens:
        return False
    overlap = len(next_tokens & current_tokens)
    return (overlap / len(next_tokens)) < _STALE_PROGRESS_OVERLAP_THRESHOLD


def _find_stale_progress_row(progress_rows: list, current_text: str):
    """Return the most recent progress row whose next_steps looks stale, or None.

    `progress_rows` is assumed ordered DESC by created_at (as task-start does).
    Only the newest non-empty next_steps is evaluated — older entries are
    superseded by design and not worth warning about separately.
    """
    for row in progress_rows:
        next_steps = row["next_steps"]
        if not next_steps or not next_steps.strip():
            continue
        if _progress_next_steps_is_stale(next_steps, current_text):
            return row
        return None
    return None


def _strip_marker_prefixes(line: str) -> str:
    stripped = line.strip()
    while True:
        updated = re.sub(r"^(?:>\s*|[-*]\s+|\d+[.)]\s+)", "", stripped).strip()
        if updated == stripped:
            return stripped
        stripped = updated


def _find_defer_trigger(text: str) -> str | None:
    """Return the first explicit defer-trigger line in task text, if any."""
    if not text:
        return None
    for raw_line in text.splitlines():
        line = _strip_marker_prefixes(raw_line)
        if not line:
            continue
        if _DEFER_TRIGGER_RE.search(line):
            return line
    return None


def _register_active_project() -> None:
    """Append the active REPO_ROOT to the active-projects registry.

    Skipped when TUSK_DB is set (explicit DB override is not tied to a repo root)
    or when the necessary env vars are missing (e.g., running outside the tusk
    wrapper).
    """
    if os.environ.get("TUSK_DB"):
        return
    registry = os.environ.get("TUSK_ACTIVE_PROJECTS_FILE")
    repo_root = os.environ.get("TUSK_REPO_ROOT")
    if not registry or not repo_root:
        return
    try:
        canon = os.path.realpath(repo_root)
        if not os.path.isdir(canon):
            return
        os.makedirs(os.path.dirname(registry), exist_ok=True)
        existing: list[str] = []
        if os.path.exists(registry):
            with open(registry, encoding="utf-8") as f:
                existing = [line.rstrip("\n") for line in f if line.strip()]
        if canon in existing:
            return
        with open(registry, "a", encoding="utf-8") as f:
            f.write(canon + "\n")
    except OSError:
        pass


def _task_commits_on_default(
    db_path: str, task_id: int, conn: sqlite3.Connection | None = None
) -> bool:
    """Return True if [TASK-<id>] commits already exist on the default branch.

    Widens task-start's deliverable_check_needed beyond the completed-criteria
    proxy (issue #948): an orphaned task whose commits already shipped to
    origin/<default> with zero criteria marked done must still trigger the
    deliverable check. Scans the local default branch and origin/<default>
    (a no-checkout fast-forward push leaves the local default behind origin).
    Best-effort — any git failure yields False.

    Prefix-collision file-overlap heuristic (issue #1056): a bare [TASK-<id>]
    message match from a prior task-numbering epoch must not flip the flag
    when the task has a scope signal the matched commits don't touch. Matched
    commits route through the shared block-level filter (issue #855) with
    ``fallthrough=False``, mirroring tusk-task-unstart's gate semantics:
    empty kept set = every block off-scope = prefix-match false positive →
    False. No scope signal, ``conn=None``, or a heuristic failure keeps the
    conservative True so genuine orphaned work is never silently dropped.

    Deliberately does NOT mirror the TASK-472 scope_enforced bypass used by
    check-deliverables and task-unstart: this scan has no ``since`` anchor,
    so a prior-epoch commit's scope-guard pass binds to the OLD task that
    owned the ID — trusting it here would reintroduce the false positive.
    """
    repo_root = os.environ.get("TUSK_REPO_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(db_path))
    )
    try:
        default = _git_helpers.default_branch(repo_root)
    except Exception:
        return False
    commits: list = []
    seen: set = set()
    for ref in (default, f"origin/{default}"):
        for sha in _git_helpers.find_task_commits(task_id, repo_root, refs=[ref]):
            if sha not in seen:
                seen.add(sha)
                commits.append(sha)
    if not commits:
        return False
    try:
        kept = _git_helpers.filter_commits_by_block_overlap(
            commits, task_id, repo_root, conn, fallthrough=False
        )
    except Exception:
        return True
    return bool(kept)


def _count_criteria_already_passing(conn: sqlite3.Connection, task_id: int) -> int:
    """Count incomplete code/file criteria whose verification specs already pass.

    Convergent-completion signal (issue #1051): a sibling task may have shipped
    this task's deliverables before pickup, leaving the disk in a state where
    automatable acceptance criteria pass before any work begins. Runs the same
    specs `tusk criteria done` executes, via its runner (timeout-bounded,
    exception-safe), read-only at start. test-type specs are excluded — full
    suite runs are too slow for task-start latency. Best-effort by design: any
    failure counts as not-passing and the whole scan degrades to 0 rather than
    blocking task-start.
    """
    try:
        rows = conn.execute(
            "SELECT criterion_type, verification_spec FROM acceptance_criteria "
            "WHERE task_id = ? AND is_completed = 0 AND is_deferred = 0 "
            "AND criterion_type IN ('code', 'file') "
            "AND verification_spec IS NOT NULL AND verification_spec != ''",
            (task_id,),
        ).fetchall()
        if not rows:
            return 0
        run_verification = tusk_loader.load("tusk-criteria").run_verification
        return sum(
            1
            for row in rows
            if run_verification(row["criterion_type"], row["verification_spec"])["passed"]
        )
    except Exception:
        return 0


def _default_branch_staleness_warning(repo_root: str | None) -> dict | None:
    """Return a non-blocking warning when local default is behind origin.

    Best-effort by design: task-start must not block just because a repo has no
    origin, fetch is unavailable, or the local checkout is offline.
    """
    if not repo_root:
        return None
    try:
        default = _git_helpers.default_branch(repo_root)
    except Exception:
        return None

    try:
        fetch_res = subprocess.run(
            ["git", "-C", repo_root, "fetch", "origin", default, "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        count_res = subprocess.run(
            [
                "git", "-C", repo_root,
                "rev-list", "--count", f"{default}..origin/{default}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if fetch_res.returncode != 0:
        return None
    if count_res.returncode != 0:
        return None
    try:
        behind_count = int(count_res.stdout.strip() or "0")
    except ValueError:
        return None
    if behind_count <= 0:
        return None
    return {
        "type": "stale_default_branch",
        "default_branch": default,
        "behind_count": behind_count,
        "message": (
            f"local {default} is {behind_count} commit(s) behind "
            f"origin/{default}; consider syncing before investigating"
        ),
    }


def _current_repo_root() -> str | None:
    repo_root = os.environ.get("TUSK_REPO_ROOT")
    if not repo_root:
        return None
    return os.path.realpath(repo_root)


def _recorded_task_workspace(conn: sqlite3.Connection, task_id: int):
    return conn.execute(
        "SELECT workspace_path FROM task_workspaces WHERE task_id = ? "
        "ORDER BY updated_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(left) == os.path.realpath(right)


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused but kept for dispatch consistency)
    parser = argparse.ArgumentParser(
        prog="tusk task-start",
        description="Begin work on a task (or pick the top ready task when no ID is given)",
    )
    parser.add_argument(
        "task_id",
        type=int,
        nargs="?",
        default=None,
        help="Task ID. Omit to auto-select the top WSJF-ranked ready task.",
    )
    parser.add_argument("--force", action="store_true", help="Bypass zero-criteria guard")
    parser.add_argument(
        "--force-deps",
        dest="force_deps",
        action="store_true",
        help="Bypass unmet 'blocks' dependency guard (use sparingly)",
    )
    parser.add_argument(
        "--force-contingent",
        dest="force_contingent",
        action="store_true",
        help="Bypass open 'contingent' dependency guard (use sparingly)",
    )
    parser.add_argument(
        "--force-not-before",
        dest="force_not_before",
        action="store_true",
        help="Bypass a future not_before timestamp (use sparingly)",
    )
    parser.add_argument(
        "--force-session",
        dest="force_session",
        action="store_true",
        help="Reuse an existing active session from outside the recorded task workspace",
    )
    parser.add_argument("--agent", dest="agent_name", metavar="NAME", help="Agent name")
    parser.add_argument(
        "--skill",
        dest="skill_name",
        metavar="NAME",
        help="Also open a skill_runs row for cost tracking (saves a follow-up 'skill-run start' call).",
    )
    args = parser.parse_args(argv[2:])
    task_id = args.task_id
    force = args.force
    force_deps = args.force_deps
    force_contingent = args.force_contingent
    force_not_before = args.force_not_before
    force_session = args.force_session
    agent_name = args.agent_name
    skill_name = args.skill_name

    conn = get_connection(db_path)
    try:
        # 0. Fused select path: no explicit task_id means "start the top
        # WSJF-ranked ready task". Mirrors tusk-task-select's exit-1 message
        # so shell-level callers (and /loop) can treat the two paths
        # interchangeably.
        if task_id is None:
            top = select_top_ready_task(conn)
            if top is None:
                print(empty_backlog_message(), file=sys.stderr)
                return 1
            task_id = top["id"]

        # 1. Fetch the task
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            print(f"Error: Task {task_id} not found", file=sys.stderr)
            return 2

        if task["status"] == "Done":
            print(f"Error: Task {task_id} is already Done", file=sys.stderr)
            return 2

        # 1a. Guard: task must not be time-gated into the future.
        # Mirrors v_ready_tasks: future not_before rows are not ready unless
        # the caller explicitly overrides the wall-clock gate.
        not_before = task["not_before"] if "not_before" in task.keys() else None
        if not_before:
            is_future = conn.execute(
                "SELECT datetime(?) > datetime('now')", (not_before,)
            ).fetchone()[0]
            if is_future:
                if not force_not_before:
                    print(
                        f"Error: Task {task_id} is deferred until {not_before}. "
                        "Start it after that time, or bypass with --force-not-before "
                        "(use sparingly).",
                        file=sys.stderr,
                    )
                    return 2
                print(
                    f"Warning: Task {task_id} is deferred until {not_before}. "
                    "Proceeding anyway due to --force-not-before.",
                    file=sys.stderr,
                )

        # 1b. Guard: task must have at least one acceptance criterion
        criteria_count = conn.execute(
            "SELECT COUNT(*) FROM acceptance_criteria WHERE task_id = ? AND is_deferred = 0",
            (task_id,),
        ).fetchone()[0]
        if criteria_count == 0:
            if not force:
                print(
                    f"Error: Task {task_id} has no acceptance criteria. "
                    f"Add at least one before starting work:\n"
                    f"  tusk criteria add {task_id} \"<criterion text>\"",
                    file=sys.stderr,
                )
                return 2
            print(
                f"Warning: Task {task_id} has no acceptance criteria. "
                f"Proceeding anyway due to --force.\n"
                f"  To add criteria: tusk criteria add {task_id} \"<criterion text>\"",
                file=sys.stderr,
            )

        # 1c. Guard: task must not be blocked by unmet 'blocks' dependencies.
        # Mirrors v_ready_tasks' hard readiness semantics for blocks-type deps.
        unmet_deps = conn.execute(
            "SELECT b.id, b.summary, b.status "
            "FROM task_dependencies d "
            "JOIN tasks b ON b.id = d.depends_on_id "
            "WHERE d.task_id = ? AND d.relationship_type = 'blocks' "
            "AND b.status <> 'Done' "
            "ORDER BY b.id",
            (task_id,),
        ).fetchall()
        if unmet_deps:
            if not force_deps:
                lines = [
                    f"Error: Task {task_id} is blocked by unmet 'blocks' dependencies:"
                ]
                for d in unmet_deps:
                    lines.append(
                        f"  • TASK-{d['id']} ({d['status']}) — {d['summary']}"
                    )
                lines.append(
                    "Finish the upstream task(s), or bypass with --force-deps "
                    "(use sparingly — 'blocks' deps exist for a reason)."
                )
                print("\n".join(lines), file=sys.stderr)
                return 2
            blocker_ids = ", ".join(f"TASK-{d['id']}" for d in unmet_deps)
            print(
                f"Warning: Task {task_id} is blocked by unmet 'blocks' deps "
                f"({blocker_ids}). Proceeding anyway due to --force-deps.",
                file=sys.stderr,
            )

        # 1d. Guard: open contingent deps are not hard blockers in the DAG, but
        # task-start should not silently hand up a task whose prerequisite path
        # has not resolved. Require an explicit bypass so the operator sees the
        # ordering risk before work begins.
        open_contingent_deps = conn.execute(
            "SELECT b.id, b.summary, b.status "
            "FROM task_dependencies d "
            "JOIN tasks b ON b.id = d.depends_on_id "
            "WHERE d.task_id = ? AND d.relationship_type = 'contingent' "
            "AND b.status <> 'Done' "
            "ORDER BY b.id",
            (task_id,),
        ).fetchall()
        if open_contingent_deps:
            if not force_contingent:
                lines = [
                    f"Error: Task {task_id} has open 'contingent' dependencies:"
                ]
                for d in open_contingent_deps:
                    lines.append(
                        f"  • TASK-{d['id']} ({d['status']}) — {d['summary']}"
                    )
                lines.append(
                    "Finish the upstream task(s), or bypass with --force-contingent "
                    "(use sparingly — contingent deps may make this task premature)."
                )
                print("\n".join(lines), file=sys.stderr)
                return 2
            contingent_ids = ", ".join(
                f"TASK-{d['id']}" for d in open_contingent_deps
            )
            print(
                f"Warning: Task {task_id} has open 'contingent' deps "
                f"({contingent_ids}). Proceeding anyway due to --force-contingent.",
                file=sys.stderr,
            )

        # 1e. Guard: task must not have open external blockers
        open_blockers = conn.execute(
            "SELECT id, description, blocker_type FROM external_blockers "
            "WHERE task_id = ? AND is_resolved = 0",
            (task_id,),
        ).fetchall()
        if open_blockers:
            lines = [f"Error: Task {task_id} has unresolved external blockers:"]
            for b in open_blockers:
                btype = f" [{b['blocker_type']}]" if b["blocker_type"] else ""
                lines.append(f"  • [{b['id']}]{btype} {b['description']}")
            lines.append("Resolve blockers with: tusk blockers resolve <blocker_id>")
            print("\n".join(lines), file=sys.stderr)
            return 2

        # 2. Check for prior progress
        progress_rows = conn.execute(
            "SELECT * FROM task_progress WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()

        # 3. Check for an open session to reuse
        open_session = conn.execute(
            "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()

        if open_session:
            session_id = open_session["id"]
            workspace = _recorded_task_workspace(conn, task_id)
            workspace_path = workspace["workspace_path"] if workspace else None
            current_root = _current_repo_root()
            if not _same_path(current_root, workspace_path):
                if not force_session:
                    lines = [
                        f"Error: Task {task_id} already has an active session "
                        f"(session {session_id})."
                    ]
                    if workspace_path:
                        lines.append(f"Recorded task workspace: {workspace_path}")
                        lines.append(
                            "Run from that workspace to continue the task, or pass "
                            "--force-session to explicitly reuse the active session."
                        )
                    else:
                        lines.append("No recorded task workspace was found for this task.")
                        lines.append(
                            "Create/reuse one with `tusk task-worktree create`, or pass "
                            "--force-session to explicitly reuse the active session."
                        )
                    if current_root:
                        lines.append(f"Current checkout: {current_root}")
                    print("\n".join(lines), file=sys.stderr)
                    return 2
                print(
                    f"Warning: Task {task_id} already has an active session "
                    f"(session {session_id}); reusing it due to --force-session.",
                    file=sys.stderr,
                )
            # Update agent_name on reused session if --agent was passed
            if agent_name is not None:
                conn.execute(
                    "UPDATE task_sessions SET agent_name = ? WHERE id = ?",
                    (agent_name, session_id),
                )
        else:
            # Create a new session. Under concurrent /chain execution two agents
            # may both read no-open-session and then race to INSERT. The partial
            # UNIQUE index on task_sessions(task_id) WHERE ended_at IS NULL will
            # reject the second INSERT; catch that and fall back to the session
            # the winning agent already created.
            try:
                conn.execute(
                    "INSERT INTO task_sessions (task_id, started_at, agent_name)"
                    " VALUES (?, datetime('now'), ?)",
                    (task_id, agent_name),
                )
                session_id = conn.execute(
                    "SELECT MAX(id) as id FROM task_sessions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()["id"]
            except sqlite3.IntegrityError:
                # Another concurrent agent just opened a session for this task.
                # Reuse it rather than failing.
                print(
                    f"Warning: concurrent session detected for task {task_id}; "
                    f"reusing existing open session.",
                    file=sys.stderr,
                )
                existing = conn.execute(
                    "SELECT id FROM task_sessions WHERE task_id = ? AND ended_at IS NULL "
                    "ORDER BY started_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if not existing:
                    print(
                        f"Error: UNIQUE violation but no open session found for task {task_id}.",
                        file=sys.stderr,
                    )
                    return 2
                session_id = existing["id"]
                if agent_name is not None:
                    conn.execute(
                        "UPDATE task_sessions SET agent_name = ? WHERE id = ?",
                        (agent_name, session_id),
                    )

        # 4. Update status to In Progress (if not already)
        if task["status"] != "In Progress":
            conn.execute(
                "UPDATE tasks SET status = 'In Progress', updated_at = datetime('now'),"
                " started_at = CASE WHEN started_at IS NULL THEN datetime('now') ELSE started_at END"
                " WHERE id = ?",
                (task_id,),
            )

        conn.commit()

        # 5. Fetch acceptance criteria
        criteria_rows = conn.execute(
            "SELECT id, task_id, criterion, source, is_completed, "
            "criterion_type, verification_spec, created_at, updated_at "
            "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()

        # 6. Build and return JSON result
        task_dict = {key: task[key] for key in task.keys()}
        task_dict["status"] = "In Progress"
        progress_list = [{key: row[key] for key in row.keys()} for row in progress_rows]
        criteria_list = [{key: row[key] for key in row.keys()} for row in criteria_rows]

        # Warn if task references unfinished prerequisite tasks.
        # The text scan picks up any TASK-N mention in this task's own
        # description/summary, but a mention does not imply direction: the
        # referenced task may be a downstream dependent (it depends_on THIS
        # task), in which case THIS task is the prerequisite and warning about
        # it is backwards (issue #956). Consult task_dependencies and drop any
        # referenced task that depends_on the current task via 'blocks' so only
        # genuine prerequisites (or un-formalized text references) are warned.
        text = (task["description"] or "") + "\n" + (task["summary"] or "")
        referenced_ids = list({
            int(m.group(1))
            for m in re.finditer(r'\bTASK-(\d+)\b', text, re.IGNORECASE)
            if int(m.group(1)) != task_id
        })
        if referenced_ids:
            placeholders = ",".join("?" * len(referenced_ids))
            warn_rows = conn.execute(
                f"SELECT id, summary FROM tasks "
                f"WHERE id IN ({placeholders}) AND status = 'To Do' "
                f"AND id NOT IN ("
                f"  SELECT d.task_id FROM task_dependencies d "
                f"  WHERE d.depends_on_id = ? AND d.relationship_type = 'blocks'"
                f")",
                referenced_ids + [task_id],
            ).fetchall()
            if warn_rows:
                print("Warning: selected task references unfinished prerequisite tasks:", file=sys.stderr)
                for wr in warn_rows:
                    print(f"  TASK-{wr['id']}: {wr['summary']}", file=sys.stderr)

        # Warn when the most recent progress.next_steps shares too little
        # vocabulary with the current summary/description — signals the task
        # was rewritten after prior checkpoints were written, making those
        # notes misleading handoff context.
        stale_row = _find_stale_progress_row(progress_list, text)
        if stale_row is not None:
            preview = " ".join((stale_row["next_steps"] or "").split())
            if len(preview) > 160:
                preview = preview[:157] + "..."
            print(
                f"Warning: prior progress for task {task_id} may be stale — "
                f"latest next_steps (from {stale_row['created_at']}) does not "
                f"substantially reference the current summary/description. "
                f"Treat as context, not authoritative handoff.",
                file=sys.stderr,
            )
            print(f"  next_steps: {preview}", file=sys.stderr)

        defer_trigger = _find_defer_trigger(text)

        deliverable_check_needed = any(c["is_completed"] for c in criteria_list)
        if not deliverable_check_needed:
            # Orphaned-work signal (issue #948): a prior session may have committed
            # and pushed [TASK-N] commits to the default branch without finalizing
            # via tusk merge or marking any criterion done. The completed-criteria
            # proxy misses that state, so scan the default branch for shipped commits.
            deliverable_check_needed = _task_commits_on_default(db_path, task_id, conn)

        # Convergent-completion signal (issue #1051): sibling work may have
        # already shipped this task's deliverables, leaving automatable
        # criteria passing on disk before any work begins.
        criteria_already_passing = _count_criteria_already_passing(conn, task_id)
        if criteria_already_passing > 0:
            deliverable_check_needed = True
            incomplete_total = sum(1 for c in criteria_list if not c["is_completed"])
            print(
                f"Warning: {criteria_already_passing}/{incomplete_total} incomplete "
                f"criteria verification spec(s) already pass — possible convergent "
                f"completion; run tusk check-deliverables {task_id} before implementing",
                file=sys.stderr,
            )

        # Optional fused skill-run start: collapses the common /tusk, /chain,
        # /review-commits, /retro pattern of calling `tusk skill-run start <name>
        # --task-id <id>` immediately after task-start into a single CLI round-trip.
        skill_run_info = None
        if skill_name is not None:
            cur = conn.execute(
                "INSERT INTO skill_runs (skill_name, task_id) VALUES (?, ?)",
                (skill_name, task_id),
            )
            conn.commit()
            run_id = cur.lastrowid
            run_row = conn.execute(
                "SELECT id, skill_name, started_at, task_id FROM skill_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            skill_run_info = {
                "run_id": run_row["id"],
                "skill_name": run_row["skill_name"],
                "started_at": run_row["started_at"],
                "task_id": run_row["task_id"],
            }

        result = {
            "task": task_dict,
            "progress": progress_list,
            "criteria": criteria_list,
            "session_id": session_id,
            "deliverable_check_needed": deliverable_check_needed,
            "criteria_already_passing": criteria_already_passing,
            "skill_run": skill_run_info,
        }
        warnings = {}
        if defer_trigger is not None:
            warnings["defer_trigger"] = {
                "type": "defer_trigger",
                "line": defer_trigger,
                "message": (
                    "task description contains an explicit defer trigger; "
                    "confirm it is satisfied before investigating"
                ),
            }
            print(
                f"Warning: task description contains defer trigger: {defer_trigger}",
                file=sys.stderr,
            )
        stale_default_warning = _default_branch_staleness_warning(_current_repo_root())
        if stale_default_warning is not None:
            warnings["stale_default_branch"] = stale_default_warning
            print(
                f"Warning: {stale_default_warning['message']}.",
                file=sys.stderr,
            )
        if warnings:
            result["warnings"] = warnings

        _register_active_project()

        print(dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-start [<task_id>] [--force] [--force-deps] [--force-contingent] [--force-not-before] [--force-session] [--agent NAME] [--skill NAME]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
