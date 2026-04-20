#!/usr/bin/env python3
"""Run the same task under two or more Claude models and compare the results.

Called by the tusk wrapper:
    tusk bakeoff <task_id> --models m1,m2[,m3...]
                 [--workspace-root <path>] [--claude-bin <path>]
                 [--timeout <seconds>] [--isolation worktree|clone] [--dry-run]
    tusk bakeoff pick <bakeoff_id> <shadow_id> [--rebase]
    tusk bakeoff discard <bakeoff_id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path (accepted for dispatch consistency, unused)
    sys.argv[3:] — command flags (or `pick`/`discard` subcommand + its args)

For each model the command:
  1. Clones the source task into a shadow row (bakeoff_shadow = 1) sharing a
     freshly-minted bakeoff_id across all attempts, copying every acceptance
     criterion verbatim.
  2. Materializes a workspace on a new branch
     `feature/bakeoff-<bakeoff_id>-<shadow_id>-<model_slug>` branched from the
     source task's default branch (resolved via `tusk git-default-branch`).
     With the default --isolation=worktree, this is a `git worktree add` that
     shares the primary repo's .git; with --isolation=clone, it is a full
     `git clone --local --no-hardlinks` so each attempt has an independent
     .git and `git log --all` cannot see sibling attempts' branches.
  3. Spawns a background `claude -p /tusk <shadow_id> --model <model>` process
     pinned to that workspace. Each agent is told by the `--append-system-prompt`
     to skip the `tusk branch` and `tusk merge` steps so the branch stays
     intact for comparison. Clone mode enforces that blindness at the
     filesystem level; worktree mode relies on the prompt alone.

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
      — pick/discard: unknown bakeoff_id, bad shadow_id, or any shadow session still open
    2 — worktree or agent spawn failed before aggregation
      — pick: git merge of the chosen shadow branch failed
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
    isolation: str = "worktree",
) -> int:
    """Insert a shadow row cloning summary/description/criteria from source.

    Shadow rows get status='To Do' so the spawned /tusk agent can start them
    normally; `bakeoff_shadow = 1` keeps them out of v_ready_tasks and the
    default task-list. The model + isolation mode are recorded in the
    description so a reader paging the DB (and pick/discard cleanup) can
    identify the attempt without consulting skill_runs.
    """
    suffix = (
        f"\n\n[bakeoff {bakeoff_id} attempt · model={model} · "
        f"isolation={isolation} · source=TASK-{source['id']}]"
    )
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


def _create_clone(
    repo_root: str,
    clone_path: str,
    branch: str,
    base_branch: str,
) -> tuple[bool, str]:
    """`git clone --local --no-hardlinks --single-branch --branch <base>` + checkout.

    Produces a fully independent .git so `git log --all` / `git branch -a`
    inside the clone only see the attempt's own refs — sibling attempts are
    unreachable at the filesystem level, not just by convention. --no-hardlinks
    ensures no shared object storage between clones (stronger isolation than
    the default --local, which hardlinks objects). --single-branch restricts
    the initial fetch to `base_branch` only — without it `git clone --branch`
    still pulls every ref from the source repo, so stale bakeoff branches
    from earlier runs would leak into the clone as origin/feature/bakeoff-*
    and become visible to `git log --all`. The feature branch is created at
    HEAD inside the clone, mirroring `git worktree add -b` semantics.
    """
    os.makedirs(os.path.dirname(clone_path), exist_ok=True)
    clone_result = subprocess.run(
        [
            "git", "clone", "--local", "--no-hardlinks", "--single-branch",
            "--branch", base_branch, repo_root, clone_path,
        ],
        capture_output=True, text=True, encoding="utf-8",
    )
    if clone_result.returncode != 0:
        return False, clone_result.stderr.strip()
    checkout = subprocess.run(
        ["git", "checkout", "-b", branch],
        capture_output=True, text=True, encoding="utf-8",
        cwd=clone_path,
    )
    if checkout.returncode != 0:
        return False, f"git checkout -b {branch}: {checkout.stderr.strip()}"
    return True, ""


def _spawn_agent(
    claude_bin: str,
    shadow_id: int,
    model: str,
    workspace_path: str,
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
        cwd=workspace_path,
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


def _collect_attempt_metrics(
    conn: sqlite3.Connection,
    attempt: dict,
    repo_root: str,
) -> dict:
    """Per-shadow aggregation reusing tusk-task-summary's cost/duration/diff/tokens helpers.

    Diff stats come from `git log --grep=[TASK-<shadow>]` across --all refs, so
    the attempt branch's commits remain in scope even if its worktree is later
    removed (the branch itself persists until manually deleted).
    """
    summary = _task_summary.build_summary(conn, attempt["shadow_id"], repo_root) or {}
    verdict = _fetch_review_verdict(conn, attempt["shadow_id"])
    return {
        **attempt,
        "cost": summary.get("cost", {"total": 0.0, "skill_run_count": 0}),
        "duration": summary.get("duration", {}),
        "diff": summary.get("diff", {}),
        "criteria": summary.get("criteria", {}),
        "tokens": summary.get("tokens", {"tokens_in": 0, "tokens_out": 0, "request_count": 0}),
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


# ---------------------------------------------------------------------------
# pick / discard cleanup subcommands
# ---------------------------------------------------------------------------


_SOURCE_ID_RE = re.compile(r"source=TASK-(\d+)")


def _extract_source_id(description: str | None) -> int | None:
    """Pull `source=TASK-<N>` out of a shadow's description suffix."""
    if not description:
        return None
    m = _SOURCE_ID_RE.search(description)
    return int(m.group(1)) if m else None


def _resolve_workspace_root(explicit: str | None) -> str:
    """Same env/default fallback as `tusk bakeoff --workspace-root`."""
    return (
        explicit
        or os.environ.get("TUSK_BAKEOFF_ROOT")
        or os.path.join(os.path.expanduser("~"), ".tusk", "bakeoffs")
    )


def _rmtree_bakeoff_workspace(workspace_root: str, bakeoff_id: int) -> bool:
    """Wholesale rmtree of `<workspace_root>/<bakeoff_id>/`. Idempotent.

    Handles clone-mode dirs (which aren't tracked by `git worktree list`) and
    also mops up any worktree dirs a prior `git worktree remove` missed.
    Returns True if the directory existed and was removed, False otherwise.
    """
    bakeoff_dir = os.path.join(workspace_root, str(bakeoff_id))
    if os.path.isdir(bakeoff_dir):
        shutil.rmtree(bakeoff_dir, ignore_errors=True)
        return True
    return False


def _find_bakeoff_branches(
    repo_root: str, bakeoff_id: int, shadow_id: int | None = None
) -> list[str]:
    """List local branches matching `feature/bakeoff-<bakeoff>-[<shadow>-]*`."""
    if shadow_id is not None:
        pattern = f"refs/heads/feature/bakeoff-{bakeoff_id}-{shadow_id}-*"
    else:
        pattern = f"refs/heads/feature/bakeoff-{bakeoff_id}-*"
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", pattern],
        capture_output=True, text=True, cwd=repo_root,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _resolve_worktree_for_branch(repo_root: str, branch: str) -> str | None:
    """Path of the worktree currently checked out on <branch>, or None.

    `git worktree list --porcelain` prints per-worktree blocks; we walk them
    recording the current `worktree <path>` line and matching when the branch
    line names the ref we care about.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    current_wt = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/"):
            if line[len("branch refs/heads/"):].strip() == branch:
                return current_wt
    return None


def _remove_worktree_and_branch(repo_root: str, branch: str) -> None:
    """Best-effort teardown: worktree first (if any), then delete the branch."""
    wt = _resolve_worktree_for_branch(repo_root, branch)
    if wt:
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt],
            capture_output=True, cwd=repo_root,
        )
        if os.path.isdir(wt):
            shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True, cwd=repo_root,
    )


def _merge_shadow_branch(
    repo_root: str, branch: str, use_rebase: bool = False
) -> tuple[bool, str]:
    """Fast-forward-only merge of <branch> into the detected default branch.

    Mirrors the local-mode steps from tusk-merge: checkout default, pull (best
    effort — skipped on unreachable remote or missing origin), ff-only merge,
    push (best effort). We don't move tasks.db aside here because bakeoff
    worktrees never commit to the primary worktree's branch, so checkout has
    nothing to overwrite; if that assumption ever breaks we inherit a clear
    git error message instead of silent data loss.

    When `use_rebase` is True, the chosen branch is rebased onto the refreshed
    default before the ff-only merge attempt — mirroring tusk-merge's
    `--rebase` path so bakeoffs started before the default branch advanced can
    still be picked without a manual rebase. A rebase conflict aborts cleanly
    and surfaces the failure instead of leaving the repo mid-rebase.
    """
    default_branch = _detect_default_branch(repo_root)

    checkout = subprocess.run(
        ["git", "checkout", default_branch],
        capture_output=True, text=True, cwd=repo_root,
    )
    if checkout.returncode != 0:
        return False, f"git checkout {default_branch}: {checkout.stderr.strip()}"

    has_origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, cwd=repo_root,
    ).returncode == 0
    if has_origin:
        subprocess.run(
            ["git", "-c", "pull.rebase=false", "pull", "origin", default_branch],
            capture_output=True, text=True, cwd=repo_root,
        )

    if use_rebase:
        co_branch = subprocess.run(
            ["git", "checkout", branch],
            capture_output=True, text=True, cwd=repo_root,
        )
        if co_branch.returncode != 0:
            return False, f"git checkout {branch}: {co_branch.stderr.strip()}"

        rebase_result = subprocess.run(
            ["git", "rebase", default_branch],
            capture_output=True, text=True, cwd=repo_root,
        )
        if rebase_result.returncode != 0:
            subprocess.run(
                ["git", "rebase", "--abort"],
                capture_output=True, cwd=repo_root,
            )
            subprocess.run(
                ["git", "checkout", default_branch],
                capture_output=True, cwd=repo_root,
            )
            return False, (
                f"git rebase {default_branch} onto {branch} failed — "
                f"resolve conflicts manually then re-run tusk bakeoff pick: "
                f"{rebase_result.stderr.strip()}"
            )

        co_back = subprocess.run(
            ["git", "checkout", default_branch],
            capture_output=True, text=True, cwd=repo_root,
        )
        if co_back.returncode != 0:
            return False, (
                f"git checkout {default_branch} after rebase: "
                f"{co_back.stderr.strip()}"
            )

    merge = subprocess.run(
        ["git", "merge", "--ff-only", branch],
        capture_output=True, text=True, cwd=repo_root,
    )
    if merge.returncode != 0:
        hint = ""
        if not use_rebase:
            hint = (
                f"\nThe bakeoff branch cannot be fast-forwarded onto "
                f"{default_branch}. Retry with:\n"
                f"  tusk bakeoff pick <bakeoff_id> <shadow_id> --rebase"
            )
        return False, f"git merge --ff-only {branch}: {merge.stderr.strip()}{hint}"

    if has_origin:
        subprocess.run(
            ["git", "push", "origin", default_branch],
            capture_output=True, text=True, cwd=repo_root,
        )
    return True, ""


def _open_shadow_sessions(
    conn: sqlite3.Connection, bakeoff_id: int
) -> list[dict]:
    """Open sessions (ended_at IS NULL) tied to any shadow of this bakeoff."""
    rows = conn.execute(
        "SELECT ts.id AS session_id, ts.task_id "
        "FROM task_sessions ts JOIN tasks t ON ts.task_id = t.id "
        "WHERE t.bakeoff_id = ? AND t.bakeoff_shadow = 1 "
        "AND ts.ended_at IS NULL "
        "ORDER BY ts.task_id, ts.id",
        (bakeoff_id,),
    ).fetchall()
    return [{"session_id": r["session_id"], "task_id": r["task_id"]} for r in rows]


def _delete_shadow_rows(conn: sqlite3.Connection, shadow_ids: list[int]) -> None:
    """Hard-delete a shadow task plus every child row that references it.

    PRAGMA foreign_keys is a per-connection pragma (OFF by default), so other
    readers of the tusk DB cannot rely on ON DELETE CASCADE firing on the
    shadow's children. Sweep the full child set explicitly: task_sessions,
    task_progress, skill_runs, code_reviews, review_comments (both the
    review_id path and the deferred_task_id back-reference), tool_call_stats,
    and tool_call_events — routed through pivot ids (session/skill_run/review)
    so rows linked only via those intermediates are cleaned too.
    """
    for sid in shadow_ids:
        session_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM task_sessions WHERE task_id = ?", (sid,)
            ).fetchall()
        ]
        skill_run_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM skill_runs WHERE task_id = ?", (sid,)
            ).fetchall()
        ]
        review_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM code_reviews WHERE task_id = ?", (sid,)
            ).fetchall()
        ]

        conn.execute("DELETE FROM tool_call_events WHERE task_id = ?", (sid,))
        conn.execute("DELETE FROM tool_call_stats  WHERE task_id = ?", (sid,))
        for tsid in session_ids:
            conn.execute(
                "DELETE FROM tool_call_events WHERE session_id = ?", (tsid,)
            )
            conn.execute(
                "DELETE FROM tool_call_stats  WHERE session_id = ?", (tsid,)
            )
        for srid in skill_run_ids:
            conn.execute(
                "DELETE FROM tool_call_events WHERE skill_run_id = ?", (srid,)
            )
            conn.execute(
                "DELETE FROM tool_call_stats  WHERE skill_run_id = ?", (srid,)
            )

        for rvid in review_ids:
            conn.execute(
                "DELETE FROM review_comments WHERE review_id = ?", (rvid,)
            )
        # review_comments.deferred_task_id has no ON DELETE clause — the FK
        # would otherwise block the parent DELETE under foreign_keys=ON.
        conn.execute(
            "DELETE FROM review_comments WHERE deferred_task_id = ?", (sid,)
        )
        conn.execute("DELETE FROM code_reviews WHERE task_id = ?", (sid,))

        conn.execute("DELETE FROM skill_runs WHERE task_id = ?", (sid,))
        conn.execute("DELETE FROM task_sessions WHERE task_id = ?", (sid,))
        conn.execute("DELETE FROM task_progress WHERE task_id = ?", (sid,))
        conn.execute("DELETE FROM acceptance_criteria WHERE task_id = ?", (sid,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (sid,))


def cmd_pick(db_path: str, config_path: str, argv: list[str]) -> int:
    """Merge a chosen shadow's branch into default, close the source task, prune siblings."""
    parser = argparse.ArgumentParser(
        prog="tusk bakeoff pick",
        description="Merge a chosen bakeoff shadow back into the source task's base branch.",
    )
    parser.add_argument("bakeoff_id", type=int)
    parser.add_argument("shadow_id", type=int)
    parser.add_argument(
        "--rebase",
        action="store_true",
        help=(
            "Rebase the chosen shadow branch onto the default branch before "
            "the ff-only merge (mirrors tusk merge --rebase). Use this when "
            "the default branch has advanced during the bakeoff and the "
            "default ff-only merge would fail."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Parent directory holding bakeoff workspaces, used to rmtree "
             "the bakeoff dir after branch teardown (clone-mode dirs aren't "
             "tracked by git worktree list). Default matches `tusk bakeoff`: "
             "$TUSK_BAKEOFF_ROOT or $HOME/.tusk/bakeoffs.",
    )
    args = parser.parse_args(argv)

    bakeoff_id = args.bakeoff_id
    shadow_id = args.shadow_id
    use_rebase = args.rebase
    workspace_root = _resolve_workspace_root(args.workspace_root)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    tusk_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")

    conn = get_connection(db_path)
    try:
        shadows = conn.execute(
            "SELECT id, description FROM tasks "
            "WHERE bakeoff_id = ? AND bakeoff_shadow = 1 ORDER BY id",
            (bakeoff_id,),
        ).fetchall()
        if not shadows:
            _print_err(f"Error: bakeoff {bakeoff_id} unknown (no shadow rows found)")
            return 1

        shadow_row = next((s for s in shadows if s["id"] == shadow_id), None)
        if shadow_row is None:
            known = [s["id"] for s in shadows]
            _print_err(
                f"Error: TASK-{shadow_id} is not a shadow of bakeoff {bakeoff_id}. "
                f"Valid shadows: {known}"
            )
            return 1

        open_sessions = _open_shadow_sessions(conn, bakeoff_id)
        if open_sessions:
            task_ids = sorted({s["task_id"] for s in open_sessions})
            _print_err(
                f"Error: bakeoff {bakeoff_id} has {len(open_sessions)} open session(s) "
                f"on shadow task(s) {task_ids}. Close them or wait for the agents to "
                f"exit before running pick."
            )
            return 1

        source_id = _extract_source_id(shadow_row["description"])
        if source_id is None:
            _print_err(
                f"Error: could not parse `source=TASK-<id>` from shadow "
                f"TASK-{shadow_id}'s description."
            )
            return 1

        source_session = conn.execute(
            "SELECT id FROM task_sessions "
            "WHERE task_id = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        source_session_id = source_session["id"] if source_session else None
        shadow_ids = [s["id"] for s in shadows]
    finally:
        conn.close()

    # Chosen branch — resolve by prefix match so we don't need to know the model slug.
    chosen_branches = _find_bakeoff_branches(repo_root, bakeoff_id, shadow_id)
    if not chosen_branches:
        _print_err(
            f"Error: no local branch matches "
            f"feature/bakeoff-{bakeoff_id}-{shadow_id}-* — "
            f"was the shadow branch already deleted?"
        )
        return 2
    if len(chosen_branches) > 1:
        _print_err(
            f"Error: multiple branches match "
            f"feature/bakeoff-{bakeoff_id}-{shadow_id}-*: {chosen_branches}. "
            f"Refusing to guess which to merge."
        )
        return 2
    chosen_branch = chosen_branches[0]

    # Must drop the chosen shadow's worktree BEFORE checking out default on the
    # primary repo — git refuses `checkout <default>` when another worktree
    # still holds the branch we'd be leaving.
    chosen_wt = _resolve_worktree_for_branch(repo_root, chosen_branch)
    if chosen_wt:
        subprocess.run(
            ["git", "worktree", "remove", "--force", chosen_wt],
            capture_output=True, cwd=repo_root,
        )
        if os.path.isdir(chosen_wt):
            shutil.rmtree(chosen_wt, ignore_errors=True)

    ok, err = _merge_shadow_branch(repo_root, chosen_branch, use_rebase=use_rebase)
    if not ok:
        _print_err(f"Error: {err}")
        return 2

    # Tear down every bakeoff branch + worktree, including the now-merged one
    # (ff-only merge leaves the ref alive — delete it so no stale handle remains).
    torn_down: list[str] = []
    for branch in _find_bakeoff_branches(repo_root, bakeoff_id):
        _remove_worktree_and_branch(repo_root, branch)
        torn_down.append(branch)

    # Clone-mode attempt dirs are not tracked by `git worktree list`, so the
    # loop above won't touch them. Rmtree the whole bakeoff workspace as a
    # catch-all — safe because the bakeoff is resolved at this point.
    workspace_removed = _rmtree_bakeoff_workspace(workspace_root, bakeoff_id)

    # Delete sibling shadow rows — keep the chosen one as an audit trail.
    deleted_shadow_ids = [sid for sid in shadow_ids if sid != shadow_id]
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _delete_shadow_rows(conn, deleted_shadow_ids)
        conn.commit()
    finally:
        conn.close()

    # Close the source task's open session (if any) and mark it Done. Mirrors
    # tusk merge's own subprocess calls into the same two downstream commands.
    session_closed = False
    if source_session_id is not None:
        sc = subprocess.run(
            [tusk_bin, "session-close", str(source_session_id)],
            capture_output=True, text=True,
        )
        session_closed = sc.returncode == 0
        if not session_closed and "already closed" not in (sc.stderr or ""):
            _print_err(
                f"Warning: session-close for session {source_session_id} exited "
                f"{sc.returncode}: {(sc.stderr or '').strip()}"
            )

    td = subprocess.run(
        [tusk_bin, "task-done", str(source_id), "--reason", "completed", "--force"],
        capture_output=True, text=True,
    )
    if td.returncode != 0:
        _print_err(
            f"Warning: task-done for TASK-{source_id} exited {td.returncode}: "
            f"{(td.stderr or '').strip()}"
        )

    print(dumps({
        "bakeoff_id": bakeoff_id,
        "shadow_id": shadow_id,
        "source_task_id": source_id,
        "merged_branch": chosen_branch,
        "deleted_shadows": deleted_shadow_ids,
        "removed_branches": torn_down,
        "workspace_removed": workspace_removed,
        "source_session_closed": session_closed,
    }))
    return 0


def cmd_discard(db_path: str, config_path: str, argv: list[str]) -> int:
    """Throw every shadow of a bakeoff away; leave the source task untouched."""
    parser = argparse.ArgumentParser(
        prog="tusk bakeoff discard",
        description="Discard every shadow of a bakeoff — delete rows and workspaces.",
    )
    parser.add_argument("bakeoff_id", type=int)
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Parent directory holding bakeoff workspaces, used to rmtree "
             "the bakeoff dir (clone-mode dirs aren't tracked by git worktree "
             "list). Default: $TUSK_BAKEOFF_ROOT or $HOME/.tusk/bakeoffs.",
    )
    args = parser.parse_args(argv)

    bakeoff_id = args.bakeoff_id
    workspace_root = _resolve_workspace_root(args.workspace_root)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    conn = get_connection(db_path)
    try:
        shadows = conn.execute(
            "SELECT id FROM tasks "
            "WHERE bakeoff_id = ? AND bakeoff_shadow = 1 ORDER BY id",
            (bakeoff_id,),
        ).fetchall()
        if not shadows:
            _print_err(f"Error: bakeoff {bakeoff_id} unknown (no shadow rows found)")
            return 1

        open_sessions = _open_shadow_sessions(conn, bakeoff_id)
        if open_sessions:
            task_ids = sorted({s["task_id"] for s in open_sessions})
            _print_err(
                f"Error: bakeoff {bakeoff_id} has {len(open_sessions)} open session(s) "
                f"on shadow task(s) {task_ids}. Close them or wait for the agents to "
                f"exit before running discard."
            )
            return 1

        shadow_ids = [s["id"] for s in shadows]
    finally:
        conn.close()

    torn_down: list[str] = []
    for branch in _find_bakeoff_branches(repo_root, bakeoff_id):
        _remove_worktree_and_branch(repo_root, branch)
        torn_down.append(branch)

    # Rmtree the whole bakeoff workspace — catches clone-mode dirs (not
    # tracked by `git worktree list`) and any stragglers from prior runs.
    workspace_removed = _rmtree_bakeoff_workspace(workspace_root, bakeoff_id)

    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _delete_shadow_rows(conn, shadow_ids)
        conn.commit()
    finally:
        conn.close()

    print(dumps({
        "bakeoff_id": bakeoff_id,
        "deleted_shadows": shadow_ids,
        "removed_branches": torn_down,
        "workspace_removed": workspace_removed,
    }))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _print_err(
            "Usage: tusk bakeoff <task_id> --models m1,m2[,m3...] "
            "[--workspace-root <path>] [--claude-bin <path>] "
            "[--isolation worktree|clone] [--dry-run]\n"
            "       tusk bakeoff pick <bakeoff_id> <shadow_id> [--workspace-root <path>]\n"
            "       tusk bakeoff discard <bakeoff_id> [--workspace-root <path>]"
        )
        return 1

    db_path = argv[0]
    config_path = argv[1]

    # pick/discard cleanup subcommands. Task IDs are always numeric or TASK-N,
    # so these literal keywords can never collide with the `run` form's
    # positional task_id.
    if len(argv) >= 3 and argv[2] == "pick":
        return cmd_pick(db_path, config_path, argv[3:])
    if len(argv) >= 3 and argv[2] == "discard":
        return cmd_discard(db_path, config_path, argv[3:])
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
        "--isolation",
        choices=["worktree", "clone"],
        default="worktree",
        help=(
            "How each attempt's workspace is materialized. 'worktree' (default) "
            "is fast and shares the primary repo's .git; agents blindness is "
            "enforced only by BAKEOFF_SYSTEM_PROMPT. 'clone' does a full "
            "`git clone --local --no-hardlinks` per attempt — slower and more "
            "disk, but agents literally cannot see sibling branches via "
            "`git log --all` or `git branch -a`."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip agent dispatch; clone shadows + create workspaces, then exit.",
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
            shadow_id = _clone_shadow(conn, source, bakeoff_id, model, args.isolation)
            model_slug = _slugify_model(model)
            branch = f"feature/bakeoff-{bakeoff_id}-{shadow_id}-{model_slug}"
            workspace_path = os.path.join(
                workspace_root, str(bakeoff_id), f"{shadow_id}-{model_slug}"
            )
            attempts.append({
                "model": model,
                "model_slug": model_slug,
                "shadow_id": shadow_id,
                "branch": branch,
                "workspace": workspace_path,
                "isolation": args.isolation,
            })
        # Hold the transaction open until workspaces succeed so a failure
        # mid-setup can roll back the shadow rows along with the directories.

        default_branch = _detect_default_branch(repo_root)
        created: list[dict] = []
        for attempt in attempts:
            if attempt["isolation"] == "clone":
                ok, err = _create_clone(
                    repo_root, attempt["workspace"], attempt["branch"], default_branch
                )
            else:
                ok, err = _create_worktree(
                    repo_root, attempt["workspace"], attempt["branch"], default_branch
                )
            if not ok:
                # Tear down workspaces we already created for this bakeoff,
                # their branches (worktree mode only — clone branches live in
                # the clone's own .git and vanish with the rmtree), and then
                # the DB rows — so a caller retrying after a fix gets a fresh
                # bakeoff_id instead of stepping over half-built state.
                for done in created:
                    if done["isolation"] == "clone":
                        if os.path.isdir(done["workspace"]):
                            shutil.rmtree(done["workspace"], ignore_errors=True)
                    else:
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", done["workspace"]],
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
                    f"Error: {attempt['isolation']} creation failed for "
                    f"{attempt['model']} (branch {attempt['branch']}): {err}. "
                    f"Rolled back {len(created)} earlier workspace(s) and {len(attempts)} shadow row(s)."
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
        "isolation": args.isolation,
        "attempts": [
            {k: v for k, v in a.items() if k in {"model", "shadow_id", "branch", "workspace", "isolation"}}
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
            attempt["workspace"],
            repo_root,
        )
        procs.append((attempt, proc))
        _print_err(
            f"Spawned agent for {attempt['model']} (TASK-{attempt['shadow_id']}) "
            f"pid={proc.pid} cwd={attempt['workspace']}"
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

    # In clone-isolation mode each attempt's branch + commits live only inside
    # its private clone's .git. Aggregation against the central DB already
    # works via TUSK_PROJECT=repo_root (skill_runs / sessions / code_reviews
    # rows land in tasks.db), but the git-based signals — pairwise diff stats
    # and fetch_diff's `git log --all --grep=[TASK-<shadow>]` scan — run
    # against repo_root and would otherwise see nothing. Fetch each clone's
    # branch into the main repo so both git-based paths behave identically to
    # worktree mode.
    for attempt in attempts:
        if attempt.get("isolation") != "clone":
            continue
        if not os.path.isdir(attempt["workspace"]):
            continue
        subprocess.run(
            ["git", "fetch", attempt["workspace"],
             f"{attempt['branch']}:{attempt['branch']}"],
            capture_output=True, text=True, encoding="utf-8", cwd=repo_root,
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
        wt = attempt.get("workspace")
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
            "[--workspace-root <path>] [--claude-bin <path>] [--dry-run]\n"
            "     tusk bakeoff pick <bakeoff_id> <shadow_id>\n"
            "     tusk bakeoff discard <bakeoff_id>",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
