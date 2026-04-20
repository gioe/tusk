#!/usr/bin/env python3
"""Run the same task under two or more Claude models and compare the results.

Called by the tusk wrapper:
    tusk bakeoff <task_id> --models m1,m2[,m3...]
                 [--workspace-root <path>] [--claude-bin <path>]
                 [--timeout <seconds>] [--dry-run]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for dispatch consistency, unused)
    sys.argv[3:] — command flags

For each model the command:
  1. Clones the source task into a shadow row (bakeoff_shadow = 1) sharing a
     freshly-minted bakeoff_id across all attempts, copying every acceptance
     criterion verbatim.
  2. Creates a git worktree on a new branch
     `feature/bakeoff-<bakeoff_id>-<shadow_id>-<model_slug>` branched from the
     source task's default branch (resolved via `tusk git-default-branch`).
  3. Spawns a background `claude -p /tusk <shadow_id> --model <model>` process
     pinned to that worktree. Each agent is blind to the other worktrees' active
     branches — the `--append-system-prompt` tells it to skip the `tusk branch`
     and `tusk merge` steps so the branch stays intact for comparison.

After every agent process exits, the command aggregates skill_runs + sessions
per shadow and emits a markdown report with one column per model covering
cost, tokens in/out, wall + active duration, request count, diff stats
(files/+lines/-lines/commits), review passes + final verdict, plus a pairwise
`git diff --stat` between every pair of attempt branches.

Workspace layout:
    <workspace_root>/<bakeoff_id>/<shadow_id>-<model_slug>

Default workspace_root is $TUSK_BAKEOFF_ROOT if set, else $HOME/.tusk/bakeoffs.

Override the `claude` binary via --claude-bin (mostly for tests): the given
path receives the same argv the real claude would, so a stub can simulate an
attempt by making a commit on the current branch.

Exit codes:
    0 — all attempts finished (some may have failed non-fatally; see report)
    1 — usage error / task not found / fewer than 2 models
    2 — worktree or agent spawn failed before aggregation
"""

import argparse
import concurrent.futures
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


def _load_task_summary_module():
    """Load tusk-task-summary.py for its cost/duration/diff aggregation helpers.

    tusk-task-summary.py already implements cost, duration, diff, and criteria
    aggregation per task — reusing it here means the bakeoff report uses the
    same definitions (e.g. `diff = commits greppable to [TASK-<id>]`) that the
    end-of-run summary does, so cross-attempt numbers stay comparable.
    """
    spec = importlib.util.spec_from_file_location(
        "tusk_task_summary",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk-task-summary.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_task_summary = _load_task_summary_module()


# Appended to each spawned agent's system prompt so it skips the two /tusk
# steps that break inside a shared-worktree branch (step 2 tries to check out
# the default branch — locked by the primary worktree — and step 12's merge
# would fold the attempt into main, destroying the side-by-side comparison).
BAKEOFF_SYSTEM_PROMPT = """\
BAKEOFF MODE — READ CAREFULLY BEFORE FOLLOWING /tusk:

You are running inside a pre-configured git worktree. A feature branch has
already been created and checked out for this task; the default branch is
locked by a sibling worktree, so you cannot use it.

1. SKIP step 2 of /tusk (`tusk branch`). The branch is already set up; running
   `tusk branch` will fail because it tries to check out the default branch
   first. Proceed directly to step 3 (explore / subagent selection).
2. SKIP step 12 of /tusk (`tusk merge` / `tusk abandon`). DO NOT run merge,
   abandon, push, or any command that moves commits off this branch. The
   branch must stay intact so the bakeoff coordinator can diff it against the
   other attempts.
3. When all acceptance criteria are satisfied, stop the workflow, call
   `tusk skill-run finish <run_id>` (run_id is in the task-start JSON), then
   exit. The coordinator handles closing the task and generating the report.

Do not `git log --all` or otherwise inspect branches outside this worktree.
Other attempts are running concurrently; staying blind to them is the point
of the bakeoff.
"""


def _slugify_model(model: str) -> str:
    """Filesystem- and branch-safe slug for a model identifier."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-").lower()
    return slug or "model"


def _resolve_task_id(raw: str) -> int:
    return int(re.sub(r"^TASK-", "", raw, flags=re.IGNORECASE))


def _parse_models(value: str) -> list[str]:
    models = [m.strip() for m in value.split(",") if m.strip()]
    return models


def _mint_bakeoff_id(conn: sqlite3.Connection) -> int:
    """Allocate the next bakeoff_id as MAX(bakeoff_id) + 1 (starting at 1)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(bakeoff_id), 0) AS m FROM tasks"
    ).fetchone()
    return int(row["m"] or 0) + 1


def _fetch_source_task(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, summary, description, priority, domain, assignee, "
        "task_type, complexity, workflow, fixes_task_id, bakeoff_shadow "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _clone_shadow(
    conn: sqlite3.Connection,
    source: dict,
    bakeoff_id: int,
    model: str,
) -> int:
    """Insert a shadow row cloning summary/description/criteria from source.

    Shadow rows get status='To Do' so the spawned /tusk agent can start them
    normally; `bakeoff_shadow = 1` keeps them out of v_ready_tasks and the
    default task-list. The model identifier is recorded in the description so
    a reader paging the DB can tell which attempt is which without consulting
    skill_runs.
    """
    suffix = f"\n\n[bakeoff {bakeoff_id} attempt · model={model} · source=TASK-{source['id']}]"
    conn.execute(
        "INSERT INTO tasks (summary, description, status, priority, domain, "
        "task_type, assignee, complexity, workflow, fixes_task_id, "
        "bakeoff_id, bakeoff_shadow, created_at, updated_at) "
        "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, ?, ?, 1, "
        "datetime('now'), datetime('now'))",
        (
            source["summary"],
            (source["description"] or "") + suffix,
            source["priority"],
            source["domain"],
            source["assignee"],
            source["task_type"],
            source["complexity"],
            source["workflow"],
            source["fixes_task_id"],
            bakeoff_id,
        ),
    )
    shadow_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO acceptance_criteria "
        "(task_id, criterion, source, criterion_type, verification_spec) "
        "SELECT ?, criterion, 'original', criterion_type, verification_spec "
        "FROM acceptance_criteria WHERE task_id = ?",
        (shadow_id, source["id"]),
    )
    return int(shadow_id)


def _detect_default_branch(repo_root: str) -> str:
    """Call `tusk git-default-branch` — its own symbolic-ref → gh → main chain."""
    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")
    result = subprocess.run(
        [tusk_bin, "git-default-branch"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def _create_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str,
    base_branch: str,
) -> tuple[bool, str]:
    """`git worktree add -b <branch> <path> <base_branch>`.

    Returns (success, stderr) so the caller can abort the bakeoff cleanly if
    any worktree fails to materialize (otherwise agents would race against a
    half-set-up workspace).
    """
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, worktree_path, base_branch],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    return result.returncode == 0, result.stderr.strip()


def _spawn_agent(
    claude_bin: str,
    shadow_id: int,
    model: str,
    worktree_path: str,
    repo_root: str,
) -> subprocess.Popen:
    """Start the Claude agent for one shadow. Does not wait."""
    env = os.environ.copy()
    # Pin tusk's REPO_ROOT to the main repo so all tusk/DB operations hit the
    # central tasks.db rather than resolving to the worktree's own path.
    env["TUSK_PROJECT"] = repo_root
    args = [
        claude_bin,
        "-p",
        f"/tusk {shadow_id}",
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        BAKEOFF_SYSTEM_PROMPT,
    ]
    return subprocess.Popen(
        args,
        cwd=worktree_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _fetch_review_verdict(conn: sqlite3.Connection, task_id: int) -> str | None:
    """Most recent code_reviews.status for the task, or None if no review ran.

    code_reviews stores the final verdict in `status` ('approved',
    'changes_requested', 'superseded', etc.) — the last row wins.
    """
    row = conn.execute(
        "SELECT status FROM code_reviews WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["status"] if row else None


def _fetch_token_totals(conn: sqlite3.Connection, task_id: int) -> dict:
    """Sum tokens_in, tokens_out, request_count across skill_runs for the task."""
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) AS tin, "
        "       COALESCE(SUM(tokens_out), 0) AS tout, "
        "       COALESCE(SUM(request_count), 0) AS req "
        "FROM skill_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return {
        "tokens_in": int(row["tin"] or 0),
        "tokens_out": int(row["tout"] or 0),
        "request_count": int(row["req"] or 0),
    }


def _collect_attempt_metrics(
    conn: sqlite3.Connection,
    attempt: dict,
    repo_root: str,
) -> dict:
    """Per-shadow aggregation reusing tusk-task-summary's cost/duration/diff helpers.

    Diff stats come from `git log --grep=[TASK-<shadow>]` across --all refs, so
    the attempt branch's commits remain in scope even if its worktree is later
    removed (the branch itself persists until manually deleted).
    """
    summary = _task_summary.build_summary(conn, attempt["shadow_id"], repo_root) or {}
    tokens = _fetch_token_totals(conn, attempt["shadow_id"])
    verdict = _fetch_review_verdict(conn, attempt["shadow_id"])
    return {
        **attempt,
        "cost": summary.get("cost", {"total": 0.0, "skill_run_count": 0}),
        "duration": summary.get("duration", {}),
        "diff": summary.get("diff", {}),
        "criteria": summary.get("criteria", {}),
        "tokens": tokens,
        "review_passes": summary.get("review_passes", 0),
        "verdict": verdict,
    }


def _pairwise_diff_stat(
    repo_root: str,
    branch_a: str,
    branch_b: str,
) -> str:
    """`git diff --stat <A>...<B>` output, or a placeholder when the ranges match."""
    result = subprocess.run(
        ["git", "diff", "--stat", f"{branch_a}...{branch_b}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode != 0:
        return f"(diff failed: {result.stderr.strip() or 'non-zero exit'})"
    return result.stdout.strip() or "(identical)"


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


def _render_report(
    bakeoff_id: int, source_task_id: int, attempts: list[dict], repo_root: str
) -> str:
    """Side-by-side markdown report: one column per attempt, plus pairwise diffs.

    Metric rows cover everything criterion 549 enumerates (cost, tokens in/out,
    wall + active time, request count, diff stats, review passes + verdict),
    followed by a `### <A> vs <B>` section for every attempt pair so the user
    can inspect how the models diverged without rerunning git themselves.
    """
    headers = ["Metric"] + [a["model"] for a in attempts]

    def _row(label: str, values: list[str]) -> str:
        return "| " + " | ".join([label] + values) + " |"

    def _cell(a: dict, key_path: list, formatter=lambda v: str(v), default="—") -> str:
        cur = a
        for k in key_path:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                return default
        return formatter(cur)

    lines: list[str] = [
        f"# Bakeoff {bakeoff_id} — TASK-{source_task_id}",
        "",
        f"Source task: TASK-{source_task_id}",
        f"Attempts: {len(attempts)}",
        "",
        "## Per-attempt metrics",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
        _row("Shadow task", [f"TASK-{a['shadow_id']}" for a in attempts]),
        _row("Branch", [a["branch"] for a in attempts]),
        _row(
            "Agent exit",
            [str(a.get("exit_code")) if a.get("exit_code") is not None else "—" for a in attempts],
        ),
        _row(
            "Cost ($)",
            [_cell(a, ["cost", "total"], lambda v: f"{float(v):.4f}") for a in attempts],
        ),
        _row("Tokens in", [_cell(a, ["tokens", "tokens_in"]) for a in attempts]),
        _row("Tokens out", [_cell(a, ["tokens", "tokens_out"]) for a in attempts]),
        _row("Requests", [_cell(a, ["tokens", "request_count"]) for a in attempts]),
        _row(
            "Wall time",
            [_format_duration(a.get("duration", {}).get("wall_seconds")) for a in attempts],
        ),
        _row(
            "Active time",
            [_format_duration(a.get("duration", {}).get("active_seconds")) for a in attempts],
        ),
        _row("Sessions", [_cell(a, ["duration", "session_count"]) for a in attempts]),
        _row("Commits", [_cell(a, ["diff", "commits"]) for a in attempts]),
        _row("Files changed", [_cell(a, ["diff", "files_changed"]) for a in attempts]),
        _row("Lines added", [_cell(a, ["diff", "lines_added"]) for a in attempts]),
        _row("Lines removed", [_cell(a, ["diff", "lines_removed"]) for a in attempts]),
        _row("Review passes", [str(a.get("review_passes", 0)) for a in attempts]),
        _row("Verdict", [a.get("verdict") or "—" for a in attempts]),
        "",
        "## Pairwise diffs",
        "",
    ]
    for i in range(len(attempts)):
        for j in range(i + 1, len(attempts)):
            a = attempts[i]
            b = attempts[j]
            lines.append(f"### {a['model']} vs {b['model']}")
            lines.append("")
            lines.append("```")
            lines.append(_pairwise_diff_stat(repo_root, a["branch"], b["branch"]))
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _print_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _print_err(
            "Usage: tusk bakeoff <task_id> --models m1,m2[,m3...] "
            "[--workspace-root <path>] [--claude-bin <path>] [--dry-run]"
        )
        return 1

    db_path = argv[0]
    # argv[1] is config_path — unused here but accepted for dispatch consistency.

    parser = argparse.ArgumentParser(
        prog="tusk bakeoff",
        description="Run the same task under N models and emit a side-by-side report.",
    )
    parser.add_argument("task_id", help="Source task ID (integer or TASK-NNN).")
    parser.add_argument(
        "--models",
        required=True,
        help="Comma-separated list of two or more Claude model identifiers.",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Parent directory for bakeoff worktrees. "
        "Default: $TUSK_BAKEOFF_ROOT or $HOME/.tusk/bakeoffs.",
    )
    parser.add_argument(
        "--claude-bin",
        default=None,
        help="Path to the claude CLI (default: $TUSK_BAKEOFF_CLAUDE_BIN or 'claude'). "
        "Tests override this to stub the agent.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("TUSK_BAKEOFF_TIMEOUT", "1800")),
        help="Per-agent wall-clock timeout in seconds (default: 1800 = 30 min). "
        "A hung agent is killed and its attempt is recorded with exit_code=-9.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip agent dispatch; clone shadows + create worktrees, then exit.",
    )
    args = parser.parse_args(argv[2:])

    try:
        source_task_id = _resolve_task_id(args.task_id)
    except ValueError:
        _print_err(f"Invalid task ID: {args.task_id}")
        return 1

    models = _parse_models(args.models)
    if len(models) < 2:
        _print_err(
            f"Error: --models needs at least 2 comma-separated model identifiers (got {len(models)})"
        )
        return 1

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    workspace_root = (
        args.workspace_root
        or os.environ.get("TUSK_BAKEOFF_ROOT")
        or os.path.join(os.path.expanduser("~"), ".tusk", "bakeoffs")
    )
    claude_bin = (
        args.claude_bin
        or os.environ.get("TUSK_BAKEOFF_CLAUDE_BIN")
        or "claude"
    )

    conn = get_connection(db_path)
    try:
        source = _fetch_source_task(conn, source_task_id)
        if source is None:
            _print_err(f"Error: task {source_task_id} not found")
            return 1
        if source["bakeoff_shadow"]:
            _print_err(
                f"Error: TASK-{source_task_id} is itself a bakeoff shadow; "
                f"point bakeoff at the original task instead."
            )
            return 1

        # BEGIN IMMEDIATE promotes this transaction to a writer up front, so
        # the MAX(bakeoff_id) read sees a consistent snapshot and another
        # concurrent bakeoff cannot mint the same id. sqlite3's default
        # isolation_level would auto-begin a DEFERRED transaction, which
        # takes no lock on the SELECT — the race is real, not theoretical.
        conn.execute("BEGIN IMMEDIATE")
        bakeoff_id = _mint_bakeoff_id(conn)
        attempts: list[dict] = []
        for model in models:
            shadow_id = _clone_shadow(conn, source, bakeoff_id, model)
            model_slug = _slugify_model(model)
            branch = f"feature/bakeoff-{bakeoff_id}-{shadow_id}-{model_slug}"
            worktree_path = os.path.join(
                workspace_root, str(bakeoff_id), f"{shadow_id}-{model_slug}"
            )
            attempts.append({
                "model": model,
                "model_slug": model_slug,
                "shadow_id": shadow_id,
                "branch": branch,
                "worktree": worktree_path,
            })
        # Hold the transaction open until worktrees succeed so a failure
        # mid-setup can roll back the shadow rows along with the worktrees.

        default_branch = _detect_default_branch(repo_root)
        created: list[dict] = []
        for attempt in attempts:
            ok, err = _create_worktree(
                repo_root, attempt["worktree"], attempt["branch"], default_branch
            )
            if not ok:
                # Tear down worktrees we already created for this bakeoff,
                # their branches, and then the DB rows — so a caller retrying
                # after a fix gets a fresh bakeoff_id instead of stepping over
                # half-built state.
                for done in created:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", done["worktree"]],
                        capture_output=True,
                        cwd=repo_root,
                    )
                    subprocess.run(
                        ["git", "branch", "-D", done["branch"]],
                        capture_output=True,
                        cwd=repo_root,
                    )
                conn.rollback()
                _print_err(
                    f"Error: worktree creation failed for {attempt['model']} "
                    f"(branch {attempt['branch']}): {err}. "
                    f"Rolled back {len(created)} earlier worktree(s) and {len(attempts)} shadow row(s)."
                )
                return 2
            created.append(attempt)

        conn.commit()
    finally:
        conn.close()

    # Dispatch metadata is informational; keep stdout reserved for the final
    # markdown report so callers can pipe it cleanly.
    _print_err(dumps({
        "bakeoff_id": bakeoff_id,
        "source_task_id": source_task_id,
        "attempts": [
            {k: v for k, v in a.items() if k in {"model", "shadow_id", "branch", "worktree"}}
            for a in attempts
        ],
    }))

    if args.dry_run:
        _print_err("--dry-run: shadows + worktrees created; skipping agent dispatch.")
        return 0

    # Spawn every agent in parallel, then wait for all of them. We can't just
    # block on subprocess.run per model because that serializes the bakeoff
    # — the whole point is to run the attempts concurrently under identical
    # wall-clock conditions.
    procs: list[tuple[dict, subprocess.Popen]] = []
    for attempt in attempts:
        proc = _spawn_agent(
            claude_bin,
            attempt["shadow_id"],
            attempt["model"],
            attempt["worktree"],
            repo_root,
        )
        procs.append((attempt, proc))
        _print_err(
            f"Spawned agent for {attempt['model']} (TASK-{attempt['shadow_id']}) "
            f"pid={proc.pid} cwd={attempt['worktree']}"
        )

    # Per-attempt wall-clock timeout: Popen.communicate blocks forever
    # otherwise, so one hung agent (infinite prompt loop, wedged subprocess,
    # stuck build) would freeze the entire bakeoff. On timeout we kill the
    # process and record exit_code=-9 so the report still emits.
    def _await(proc: subprocess.Popen) -> tuple[bytes, bytes, int]:
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout)
            return stdout or b"", stderr or b"", proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b""
            return stdout or b"", stderr or b"", -9

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(procs)) as pool:
        futures = {pool.submit(_await, p): (a, p) for (a, p) in procs}
        for fut in concurrent.futures.as_completed(futures):
            attempt, proc = futures[fut]
            try:
                stdout, stderr, rc = fut.result()
            except Exception as e:  # pragma: no cover — defensive
                attempt["exit_code"] = -1
                attempt["agent_stderr"] = str(e)
                continue
            attempt["exit_code"] = rc
            attempt["agent_stdout_tail"] = stdout.decode("utf-8", errors="replace")[-500:]
            attempt["agent_stderr_tail"] = stderr.decode("utf-8", errors="replace")[-500:]
            suffix = " (killed on timeout)" if rc == -9 else ""
            _print_err(
                f"Agent for {attempt['model']} (TASK-{attempt['shadow_id']}) "
                f"exited with code {rc}{suffix}"
            )

    # Aggregation reads the central DB after every agent has finished writing
    # to it, so every skill_runs / task_sessions / code_reviews row the agents
    # produced is visible here.
    conn = get_connection(db_path)
    try:
        enriched = [_collect_attempt_metrics(conn, a, repo_root) for a in attempts]
    finally:
        conn.close()

    report = _render_report(bakeoff_id, source_task_id, enriched, repo_root)
    print(report)

    return 0


def cleanup_worktrees(attempts: list[dict], repo_root: str) -> None:
    """Best-effort teardown: `git worktree remove --force` + rmtree the parent.

    Not wired into the default flow because the user may want to inspect the
    attempt worktrees (or re-run the aggregation) after the report emits.
    Exposed for the integration test which relies on deterministic cleanup.
    """
    for attempt in attempts:
        wt = attempt.get("worktree")
        if not wt:
            continue
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt],
            capture_output=True,
            cwd=repo_root,
        )
        if os.path.isdir(wt):
            shutil.rmtree(wt, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print(
            "Use: tusk bakeoff <task_id> --models m1,m2[,m3...] "
            "[--workspace-root <path>] [--claude-bin <path>] [--dry-run]",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
