#!/usr/bin/env python3
"""Manage code reviews for tusk tasks.

Called by the tusk wrapper:
    tusk review start|begin|add-comment|list|resolve|approve|request-changes|backfill-cost|status|summary ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, tusk-review-diff-range.py, tusk-pricing-lib.py, tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
reject_shell_metacharacters = _git_helpers.reject_shell_metacharacters

_pricing_lib = None  # populated lazily by _load_pricing_lib()


def _reject_review_note(note: str | None) -> str | None:
    """Return a diagnostic if ``note`` carries shell-substitution metacharacters,
    else None. Shared by resolve/approve/request-changes (issue #1107 — extends
    the issue #881/#1106 guard). An empty string (the "clear note" sentinel) and
    None both pass — neither carries a metacharacter to expand."""
    if not note:
        return None
    ok, diagnostic = reject_shell_metacharacters(note, subject="review note")
    return None if ok else diagnostic


def _load_pricing_lib():
    """Load the pricing library on first use.

    Lazy so test paths that never touch cost capture don't pay for the
    import (and so we can monkeypatch this hook in tests to inject a stub).
    """
    global _pricing_lib
    if _pricing_lib is None:
        _pricing_lib = tusk_loader.load("tusk-pricing-lib")
        _pricing_lib.load_pricing()
    return _pricing_lib


def _compute_review_cost_from_window(created_at: str) -> dict | None:
    """Aggregate the transcript between *created_at* and now; return cost+tokens.

    Mirrors the cost computation in `tusk skill-run finish` but uses the
    review row's `created_at` as the window start. Returns None when no
    transcript is discoverable or the window contains zero requests —
    callers leave the columns NULL in that case rather than writing zeros,
    so a missing transcript stays distinguishable from a real $0 review.
    """
    lib = _load_pricing_lib()
    started_at = lib.parse_sqlite_timestamp(created_at)
    transcript_path = lib.find_transcript()
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    totals = lib.aggregate_session(transcript_path, started_at, None)
    if totals.get("request_count", 0) == 0:
        return None
    return {
        "cost_dollars": lib.compute_cost(totals),
        "tokens_in": lib.compute_tokens_in(totals),
        "tokens_out": totals["output_tokens"],
        "model": totals.get("model"),
    }


def _add_cost_flags(parser: argparse.ArgumentParser) -> None:
    """Wire the cost-capture flags onto an approve/request-changes subparser."""
    parser.add_argument(
        "--cost-dollars",
        dest="cost_dollars",
        type=float,
        default=None,
        help="Override the auto-computed cost (USD) for this review row.",
    )
    parser.add_argument(
        "--tokens-in",
        dest="tokens_in",
        type=int,
        default=None,
        help="Override the auto-computed tokens_in count for this review row.",
    )
    parser.add_argument(
        "--tokens-out",
        dest="tokens_out",
        type=int,
        default=None,
        help="Override the auto-computed tokens_out count for this review row.",
    )
    parser.add_argument(
        "--skip-cost",
        dest="skip_cost",
        action="store_true",
        help="Skip transcript-based cost auto-compute on this call.",
    )


def _resolve_cost_columns(args, created_at: str) -> tuple:
    """Return (cost_dollars, tokens_in, tokens_out) to set on the review row.

    Per-column priority: explicit --cost-dollars/--tokens-in/--tokens-out
    flags override; otherwise auto-compute from the transcript window. A
    None in any slot means "leave the column alone." `--skip-cost` short-
    circuits the auto-compute so callers (bakeoffs, manual fixes) can
    disable it without passing all three explicit values.
    """
    explicit_cost = getattr(args, "cost_dollars", None)
    explicit_tin = getattr(args, "tokens_in", None)
    explicit_tout = getattr(args, "tokens_out", None)
    skip_cost = getattr(args, "skip_cost", False)

    if skip_cost or all(x is not None for x in (explicit_cost, explicit_tin, explicit_tout)):
        return explicit_cost, explicit_tin, explicit_tout

    computed = _compute_review_cost_from_window(created_at)
    if computed is None:
        return explicit_cost, explicit_tin, explicit_tout

    return (
        explicit_cost if explicit_cost is not None else computed["cost_dollars"],
        explicit_tin if explicit_tin is not None else computed["tokens_in"],
        explicit_tout if explicit_tout is not None else computed["tokens_out"],
    )


def load_review_config(config_path: str) -> dict:
    """Load review-related config values."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        return {
            "reviewer": config.get("review", {}).get("reviewer"),
            "max_passes": config.get("review", {}).get("max_passes", 2),
            "categories": config.get("review_categories", []),
            "severities": config.get("review_severities", []),
        }
    except (OSError, json.JSONDecodeError):
        return {"reviewer": None, "max_passes": 2, "categories": [], "severities": []}


def cmd_start(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Create one code_reviews row for the configured reviewer (or unassigned)."""
    if args.diff_summary is not None and not args.diff_summary.strip():
        print(
            "Error: --diff-summary must not be empty or whitespace-only. "
            "Either omit the flag or pass a non-empty summary.",
            file=sys.stderr,
        )
        return 1

    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        # Supersede any existing pending reviews from prior passes before creating new ones
        prior_pending = conn.execute(
            "SELECT id FROM code_reviews WHERE task_id = ? AND status = 'pending'",
            (args.task_id,),
        ).fetchall()
        if prior_pending:
            conn.execute(
                "UPDATE code_reviews SET status = 'superseded', updated_at = datetime('now')"
                " WHERE task_id = ? AND status = 'pending'",
                (args.task_id,),
            )
            conn.commit()
            superseded_ids = ", ".join(f"#{r['id']}" for r in prior_pending)
            print(f"Superseded {len(prior_pending)} prior pending review(s): {superseded_ids}")

        cfg = load_review_config(config_path)
        reviewer_item = cfg["reviewer"]

        # CLI override wins over config
        if args.reviewer:
            reviewer_name = args.reviewer
        elif isinstance(reviewer_item, dict):
            reviewer_name = reviewer_item.get("name")
        elif isinstance(reviewer_item, str):
            reviewer_name = reviewer_item
        else:
            reviewer_name = None

        conn.execute(
            "INSERT INTO code_reviews (task_id, reviewer, status, review_pass, diff_summary, agent_name)"
            " VALUES (?, ?, 'pending', ?, ?, ?)",
            (args.task_id, reviewer_name, args.pass_num, args.diff_summary, args.agent),
        )
        conn.commit()
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    reviewer_str = f" (reviewer: {reviewer_name})" if reviewer_name else ""
    print(f"Started review #{rid} for task #{args.task_id}{reviewer_str}: {task['summary']}")

    return 0


def cmd_begin(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Bundle review-diff-range and review start into one call.

    Computes the diff range for the task, then creates a code_reviews row with
    the captured summary baked in. Returns combined JSON on stdout so callers
    never have to extract the diff summary from JSON in shell — the field most
    likely to break the `echo "$VAR" | jq` quoting hazard is no longer in the
    output. Output keys: review_id, task_id, reviewer, range, diff_lines,
    diff_lines_meaningful, recovered_from_task_commits.

    ``diff_lines_meaningful`` is the lockfile-subtracted line count and is
    the value consumers should use when deciding inline-vs-agent routing.
    ``diff_lines`` is preserved unchanged for backward compatibility (issue
    #761).
    """
    diff_range_mod = tusk_loader.load("tusk-review-diff-range")
    repo_root = diff_range_mod.resolve_repo_root(db_path)

    try:
        diff_payload = diff_range_mod.compute_range(args.task_id, repo_root, db_path)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return 1

    diff_summary = diff_payload["summary"]

    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        conn.execute(
            "UPDATE code_reviews SET status = 'superseded', updated_at = datetime('now')"
            " WHERE task_id = ? AND status = 'pending'",
            (args.task_id,),
        )
        conn.commit()

        cfg = load_review_config(config_path)
        reviewer_item = cfg["reviewer"]
        if args.reviewer:
            reviewer_name = args.reviewer
        elif isinstance(reviewer_item, dict):
            reviewer_name = reviewer_item.get("name")
        elif isinstance(reviewer_item, str):
            reviewer_name = reviewer_item
        else:
            reviewer_name = None

        conn.execute(
            "INSERT INTO code_reviews"
            " (task_id, reviewer, status, review_pass, diff_summary, diff_range, agent_name)"
            " VALUES (?, ?, 'pending', ?, ?, ?, ?)",
            (
                args.task_id,
                reviewer_name,
                args.pass_num,
                diff_summary,
                diff_payload["range"],
                args.agent,
            ),
        )
        conn.commit()
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    print(dumps({
        "review_id": rid,
        "task_id": args.task_id,
        "reviewer": reviewer_name,
        "range": diff_payload["range"],
        "diff_lines": diff_payload["diff_lines"],
        "diff_lines_meaningful": diff_payload.get(
            "diff_lines_meaningful", diff_payload["diff_lines"]
        ),
        "recovered_from_task_commits": diff_payload["recovered_from_task_commits"],
    }))
    return 0


def cmd_add_comment(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Insert a review_comments row."""
    # Reject shell-substitution metacharacters in the comment text before any DB
    # write (issue #1107 — extends the issue #881/#1106 guard). zsh/bash expand
    # `, $(...), ${...}, and bare $IDENT before tusk sees the argv, even inside
    # double quotes, silently corrupting the stored comment.
    if args.comment:
        ok, diagnostic = reject_shell_metacharacters(args.comment, subject="review comment")
        if not ok:
            print(diagnostic, file=sys.stderr)
            return 1

    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer FROM code_reviews WHERE id = ?", (args.review_id,)
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        cfg = load_review_config(config_path)

        if args.category:
            valid_cats = cfg["categories"]
            if valid_cats and args.category not in valid_cats:
                print(
                    f"Error: Invalid category '{args.category}'. Valid: {', '.join(valid_cats)}",
                    file=sys.stderr,
                )
                return 2

        if args.severity:
            valid_sevs = cfg["severities"]
            if valid_sevs and args.severity not in valid_sevs:
                print(
                    f"Error: Invalid severity '{args.severity}'. Valid: {', '.join(valid_sevs)}",
                    file=sys.stderr,
                )
                return 2

        conn.execute(
            "INSERT INTO review_comments"
            " (review_id, file_path, line_start, line_end, category, severity, comment, resolution)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                args.review_id,
                args.file,
                args.line_start,
                args.line_end,
                args.category,
                args.severity,
                args.comment,
            ),
        )
        conn.commit()

        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    loc = ""
    if args.file:
        loc = f" in {args.file}"
        if args.line_start:
            loc += f":{args.line_start}"
    cat_sev = ""
    if args.category or args.severity:
        parts = [x for x in [args.category, args.severity] if x]
        cat_sev = f" [{'/'.join(parts)}]"

    print(f"Added comment #{cid} to review #{args.review_id}{loc}{cat_sev}: {args.comment[:60]}")
    return 0


def cmd_list(args: argparse.Namespace, db_path: str) -> int:
    """Show reviews for a task, grouped by reviewer and category."""
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        reviews = conn.execute(
            "SELECT id, reviewer, status, review_pass, created_at"
            " FROM code_reviews WHERE task_id = ? AND status <> 'superseded' ORDER BY id",
            (args.task_id,),
        ).fetchall()

        # Fetch all comments in a single pass to avoid re-opening the connection
        review_ids = [rev["id"] for rev in reviews]
        all_comments: dict[int, list] = {rid: [] for rid in review_ids}
        if review_ids:
            placeholders = ",".join("?" * len(review_ids))
            for c in conn.execute(
                f"SELECT id, review_id, file_path, line_start, category, severity, comment, resolution, resolution_note"
                f" FROM review_comments WHERE review_id IN ({placeholders}) ORDER BY review_id, category, id",
                review_ids,
            ).fetchall():
                all_comments[c["review_id"]].append(c)
    finally:
        conn.close()

    if not _json_lib.pretty_requested():
        payload = [
            {
                "id": rev["id"],
                "reviewer": rev["reviewer"],
                "status": rev["status"],
                "review_pass": rev["review_pass"],
                "created_at": rev["created_at"],
                "comments": [
                    {
                        "id": c["id"],
                        "file_path": c["file_path"],
                        "line_start": c["line_start"],
                        "category": c["category"],
                        "severity": c["severity"],
                        "comment": c["comment"],
                        "resolution": c["resolution"],
                        "resolution_note": c["resolution_note"],
                    }
                    for c in all_comments.get(rev["id"], [])
                ],
            }
            for rev in reviews
        ]
        print(dumps(payload))
        return 0

    if not reviews:
        print(f"No reviews for task #{args.task_id}: {task['summary']}")
        return 0

    print(f"Reviews for task #{args.task_id}: {task['summary']}")
    print()

    for rev in reviews:
        reviewer_label = rev["reviewer"] or "(unassigned)"
        print(f"  Review #{rev['id']} — {reviewer_label} | status: {rev['status']} | pass {rev['review_pass']} | {rev['created_at']}")

        comments = all_comments.get(rev["id"], [])

        if not comments:
            print("    (no comments)")
            continue

        current_cat = None
        for c in comments:
            cat = c["category"] or "general"
            if cat != current_cat:
                print(f"\n    [{cat.upper()}]")
                current_cat = cat
            loc = ""
            if c["file_path"]:
                loc = f" {c['file_path']}"
                if c["line_start"]:
                    loc += f":{c['line_start']}"
            sev = f"[{c['severity']}] " if c["severity"] else ""
            if c["resolution"] is not None:
                note = f": {c['resolution_note']}" if c["resolution_note"] else ""
                res = f" ({c['resolution']}{note})"
            else:
                res = ""
            print(f"    #{c['id']}{loc}: {sev}{c['comment']}{res}")

        print()

    return 0


# Known file extensions surfaced in code review prose. Restricting to a
# closed set keeps false positives down: word-shaped tokens like "e.g."
# or version strings like "1.2.3" do not match.
_PATH_TOKEN_EXTENSIONS = (
    "py|md|js|ts|tsx|jsx|sh|html|css|scss|sass|less|yml|yaml|json|toml|"
    "go|rs|java|kt|swift|rb|c|cpp|cc|h|hpp|cs|sql|txt|cfg|ini|xml|env|"
    "hcl|tf|gradle|properties|lock|mjs|cjs|vue|svelte|php|pl|pm|lua|dart|"
    "ex|exs|erl|hs|clj|cljs|fs|fsx|scala|m|mm|proto|graphql|gql|conf"
)
_PATH_TOKEN_RE = re.compile(
    rf"(?<![\w/.])([\w./\-]+\.(?:{_PATH_TOKEN_EXTENSIONS}))(?![\w/])",
    re.IGNORECASE,
)
_SYMBOL_TOKEN_RE = re.compile(r"\b([A-Za-z_][\w]*\.[A-Za-z_][\w]*)\b")

# Dotted English prose abbreviations that the symbol regex above matches but
# which are never code symbols. Without this denylist the line-symbol-mismatch
# guard (issue #1012) extracted tokens like "e.g" from comment bodies such as
# "(e.g. one selling stand-up)" and auto-dismissed correctly-anchored review
# comments because the prose token was absent from the cited line but present
# elsewhere in the file (issue #1117). Compared case-insensitively.
_PROSE_ABBREVIATIONS = frozenset({
    "e.g",
    "i.e",
    "et.al",
    "a.k",  # from "a.k.a" — the regex yields only the first dotted pair
})


def _extract_paths(text: str | None) -> list[str]:
    """Extract file-path-shaped tokens from a comment body.

    Only the closed extension set above is recognized. Tokens are
    returned in order of first appearance with duplicates removed and
    trailing punctuation stripped.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_TOKEN_RE.finditer(text):
        token = m.group(1).rstrip(".,;:!?)\"'")
        if token and token not in seen:
            seen.add(token)
            found.append(token)
    return found


def _extract_symbol_tokens(text: str | None) -> list[str]:
    """Extract dotted code-symbol references from review prose.

    Common dotted English prose abbreviations (``e.g``, ``i.e``, ...) are
    excluded so the line-symbol-mismatch guard does not auto-dismiss a
    correctly-anchored comment whose body merely contains "(e.g. ...)"
    (issue #1117).
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _SYMBOL_TOKEN_RE.finditer(text):
        token = m.group(1).rstrip(".,;:!?)\"'")
        if token.lower() in _PROSE_ABBREVIATIONS:
            continue
        if token and token not in seen:
            seen.add(token)
            found.append(token)
    return found


def _read_repo_file_lines(repo_root: str, path: str) -> list[str]:
    try:
        with open(os.path.join(repo_root, path), "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _symbol_in_line(symbol: str, line: str) -> bool:
    """Return True when *symbol* appears in *line* as a whole identifier.

    The dotted symbol is matched with identifier boundaries on both sides
    rather than as a raw substring: neither the character before nor the
    character after the match may be a word character or a dot. This keeps
    a coincidental substring (``a.b`` inside ``data.bar``) from counting as
    the symbol appearing — the substring match was the primary source of
    false dismissals the prose denylist could only patch one case at a time
    (issue #1121). ``re.escape`` neutralizes the literal ``.`` in the token.
    """
    return re.search(rf"(?<![\w.]){re.escape(symbol)}(?![\w.])", line) is not None


def _line_symbol_mismatch(
    repo_root: str,
    path: str,
    line_start: int | None,
    comment: str | None,
) -> tuple[str, str] | None:
    """Return (symbol, cited_line_text) when a comment cites the wrong line.

    The guard is intentionally conservative: dismiss only when the exact
    dotted symbol is absent from the cited line but present elsewhere in
    the same file, matched as a whole identifier rather than a substring
    (issue #1121). If the symbol cannot be found as a whole token
    elsewhere, the validator leaves the finding open for the operator.
    """
    if not line_start:
        return None
    symbols = _extract_symbol_tokens(comment)
    if not symbols:
        return None
    lines = _read_repo_file_lines(repo_root, path)
    if not lines or line_start < 1 or line_start > len(lines):
        return None
    cited_line = lines[line_start - 1]
    for symbol in symbols:
        if _symbol_in_line(symbol, cited_line):
            continue
        if any(
            _symbol_in_line(symbol, line)
            for i, line in enumerate(lines)
            if i != line_start - 1
        ):
            return symbol, cited_line.strip()
    return None


def _path_in_diff(token: str, diff_files: set[str]) -> bool:
    """Decide whether *token* matches any entry in *diff_files*.

    Multi-segment tokens (containing ``/``) must match a diff file by
    full path equality — a confabulated ``apps/foo/bar.py`` is not
    rescued by a same-basename file elsewhere in the diff. Bare
    basenames match any diff file with that basename.
    """
    if token in diff_files:
        return True
    if "/" in token:
        return False
    return any(os.path.basename(f) == token for f in diff_files)


def _existing_repo_paths(token: str, repo_root: str, repo_files: set[str]) -> list[str]:
    """Return tracked/on-disk repo paths matching *token*.

    Multi-segment tokens must match their exact repo-relative path. Bare
    basenames may match any tracked file with that basename, mirroring
    ``_path_in_diff`` while still avoiding a full filesystem walk.
    """
    if "/" in token:
        if token in repo_files or os.path.exists(os.path.join(repo_root, token)):
            return [token]
        return []
    matches = sorted(p for p in repo_files if os.path.basename(p) == token)
    if os.path.exists(os.path.join(repo_root, token)) and token not in matches:
        matches.insert(0, token)
    return matches


def cmd_validate_comments(args: argparse.Namespace, db_path: str) -> int:
    """Cross-check pending review comments against the actual diff (issues #783, #912).

    The reviewer agent occasionally confabulates findings that reference
    files outside the diff — paths, behavior, or migrations that never
    landed on the branch. This helper enforces an objective ground truth:
    each pending comment's ``file_path`` must appear in
    ``git diff --name-only <range>`` (range re-derived via the same
    diff-range helper ``tusk review begin`` uses, so worktree-aware fallback
    applies). Comments naming a path not in that list are auto-resolved as
    ``dismissed`` with an explanatory ``resolution_note`` so the audit trail
    still records the fabrication.

    General-scope comments (``file_path`` is null/empty) are body-scanned
    for file-path-shaped tokens (issue #912). When the body cites one or
    more path tokens AND none of them appear in the diff, tokens that do
    not resolve to real repo files are dismissed under the fabrication
    guard. Tokens that resolve to real repo files are preserved and
    returned separately as out-of-diff real paths so the orchestrator can
    spin them off as follow-up work instead of treating them as noise.

    JSON output:
        {
            "review_id": int,
            "range": str,
            "validated": int,                # pending comments inspected
            "dismissed": [{"comment_id", "file_path"}, ...],
            "dismissed_general": [{"comment_id", "cited_paths"}, ...],
            "dismissed_symbol_mismatch": [{"comment_id", "file_path", "line_start", "symbol"}, ...],
            "out_of_diff_real": [{"comment_id", "cited_paths", "existing_paths"}, ...],
            "in_diff": int,                  # file_path values matched
            "general": int,                  # null-file_path comments preserved
            "diff_files": [str, ...],        # the diff's --name-only set
        }
    """
    diff_range_mod = tusk_loader.load("tusk-review-diff-range")

    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, diff_range FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2
        task_id = review["task_id"]
        stored_range = review["diff_range"]

        pending = conn.execute(
            "SELECT id, file_path, line_start, comment, category FROM review_comments"
            " WHERE review_id = ? AND resolution IS NULL",
            (args.review_id,),
        ).fetchall()
    finally:
        conn.close()

    repo_root = diff_range_mod.resolve_repo_root(db_path)

    # Issue #847: prefer the range stamped at review-begin time. The validator
    # used to re-derive via compute_range every call, which drifts when new
    # commits land between begin and validate-comments or the worktree
    # fallback resolves into a different checkout. Reuse keeps the validator
    # in lockstep with the range used to record findings. Fall back to
    # compute_range only when the row predates migration 69 and has no
    # stored value.
    if stored_range:
        diff_range = stored_range
        diff_cwd = repo_root
    else:
        try:
            payload = diff_range_mod.compute_range(task_id, repo_root, db_path)
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code, file=sys.stderr)
            return 1
        diff_range = payload["range"]
        # Issue #821 / TASK-412: compute_range may have re-resolved into a
        # sibling worktree to locate the feature branch. Re-run `git diff`
        # in that same checkout so the file list matches the chosen range;
        # using the orchestrator's CWD-derived repo_root here would silently
        # dismiss every legitimate finding when the primary checkout has
        # unpushed local-default commits.
        diff_cwd = payload.get("resolved_repo_root") or repo_root

    name_only = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=diff_cwd,
    )
    if name_only.returncode != 0:
        print(
            f"git diff --name-only {diff_range} failed: {name_only.stderr}",
            file=sys.stderr,
        )
        return 1
    diff_files = {p.strip() for p in name_only.stdout.splitlines() if p.strip()}
    ls_files = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=diff_cwd,
    )
    if ls_files.returncode != 0:
        print(f"git ls-files failed: {ls_files.stderr}", file=sys.stderr)
        return 1
    repo_files = {p for p in ls_files.stdout.split("\0") if p}

    dismissed = []
    dismissed_general = []
    dismissed_symbol_mismatch = []
    out_of_diff_real = []
    in_diff = 0
    general = 0
    for c in pending:
        fp = c["file_path"]
        if fp is None or fp == "":
            # Issue #912: scan the body for path-shaped tokens. A general
            # comment that cites only out-of-diff paths is dismissed under
            # the same fabrication-guard rationale; one that cites at
            # least one in-diff path, or cites no path tokens at all, is
            # preserved for the orchestrator's diff-line-quote rule.
            cited_paths = _extract_paths(c["comment"])
            if not cited_paths:
                general += 1
                continue
            if any(_path_in_diff(p, diff_files) for p in cited_paths):
                general += 1
                continue
            existing_paths = []
            for path in cited_paths:
                existing_paths.extend(
                    p for p in _existing_repo_paths(path, diff_cwd, repo_files)
                    if p not in existing_paths
                )
            if existing_paths:
                general += 1
                out_of_diff_real.append({
                    "comment_id": c["id"],
                    "cited_paths": cited_paths,
                    "existing_paths": existing_paths,
                })
                continue
            note = (
                f"validation: general comment cites paths {cited_paths} "
                f"— none present in diff range '{diff_range}' "
                f"(issue #912 fabrication guard)"
            )
            conn = get_connection(db_path)
            try:
                conn.execute(
                    "UPDATE review_comments SET resolution = 'dismissed',"
                    " resolution_note = ?, updated_at = datetime('now')"
                    " WHERE id = ?",
                    (note, c["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            dismissed_general.append({
                "comment_id": c["id"],
                "cited_paths": cited_paths,
            })
            continue
        if fp in diff_files:
            mismatch = _line_symbol_mismatch(
                diff_cwd,
                fp,
                c["line_start"],
                c["comment"],
            )
            if mismatch:
                symbol, cited_line = mismatch
                note = (
                    f"validation: cited line {c['line_start']} in '{fp}' "
                    f"does not contain referenced symbol '{symbol}' "
                    f"(line text: {cited_line!r}); symbol appears elsewhere "
                    f"in the same file (issue #1012 line-symbol-mismatch guard)"
                )
                conn = get_connection(db_path)
                try:
                    conn.execute(
                        "UPDATE review_comments SET resolution = 'dismissed',"
                        " resolution_note = ?, updated_at = datetime('now')"
                        " WHERE id = ?",
                        (note, c["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
                dismissed_symbol_mismatch.append({
                    "comment_id": c["id"],
                    "file_path": fp,
                    "line_start": c["line_start"],
                    "symbol": symbol,
                })
                continue
            in_diff += 1
            continue
        # Path is non-null and not in the diff — fabrication. Dismiss.
        note = (
            f"validation: file_path '{fp}' not present in diff range "
            f"'{diff_range}' (issue #783 fabrication guard)"
        )
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE review_comments SET resolution = 'dismissed',"
                " resolution_note = ?, updated_at = datetime('now')"
                " WHERE id = ?",
                (note, c["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        dismissed.append({"comment_id": c["id"], "file_path": fp})

    print(dumps({
        "review_id": args.review_id,
        "range": diff_range,
        "validated": len(pending),
        "dismissed": dismissed,
        "dismissed_general": dismissed_general,
        "dismissed_symbol_mismatch": dismissed_symbol_mismatch,
        "out_of_diff_real": out_of_diff_real,
        "in_diff": in_diff,
        "general": general,
        "diff_files": sorted(diff_files),
    }))
    return 0


def cmd_resolve(args: argparse.Namespace, db_path: str) -> int:
    """Update a comment's resolution field."""
    valid_resolutions = ("fixed", "dismissed")
    if args.resolution not in valid_resolutions:
        valid_str = ", ".join(valid_resolutions)
        print(
            f"Error: Invalid resolution '{args.resolution}'. Valid: {valid_str}",
            file=sys.stderr,
        )
        return 2

    diag = _reject_review_note(args.note)
    if diag is not None:
        print(diag, file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        comment = conn.execute(
            "SELECT id, comment, resolution FROM review_comments WHERE id = ?",
            (args.comment_id,),
        ).fetchone()
        if not comment:
            print(f"Error: Comment {args.comment_id} not found", file=sys.stderr)
            return 2

        set_clauses = ["resolution = ?", "updated_at = datetime('now')"]
        params: list = [args.resolution]
        if args.note is not None:
            set_clauses.append("resolution_note = ?")
            params.append(args.note or None)
        params.append(args.comment_id)
        conn.execute(
            f"UPDATE review_comments SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    note_str = f" ({args.note})" if args.note else ""
    print(f"Comment #{args.comment_id} marked '{args.resolution}'{note_str}: {comment['comment'][:60]}")
    return 0


def cmd_approve(args: argparse.Namespace, db_path: str) -> int:
    """Set code_reviews.status = 'approved' and review_pass = 1."""
    diag = _reject_review_note(args.note)
    if diag is not None:
        print(diag, file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer, status, created_at FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        cost_dollars, tokens_in, tokens_out = _resolve_cost_columns(args, review["created_at"])

        set_clauses = ["status = 'approved'", "review_pass = 1", "updated_at = datetime('now')"]
        params: list = []
        if args.note is not None:
            set_clauses.append("note = ?")
            params.append(args.note or None)
        if args.model is not None:
            set_clauses.append("model = ?")
            params.append(args.model or None)
        if cost_dollars is not None:
            set_clauses.append("cost_dollars = ?")
            params.append(cost_dollars)
        if tokens_in is not None:
            set_clauses.append("tokens_in = ?")
            params.append(tokens_in)
        if tokens_out is not None:
            set_clauses.append("tokens_out = ?")
            params.append(tokens_out)
        params.append(args.review_id)
        conn.execute(
            f"UPDATE code_reviews SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    reviewer_str = f" by {review['reviewer']}" if review["reviewer"] else ""
    if args.note == "":
        note_str = " (note cleared)"
    elif args.note:
        note_str = f" ({args.note})"
    else:
        note_str = ""
    print(f"Review #{args.review_id} approved{reviewer_str} for task #{review['task_id']}{note_str}")
    return 0


def cmd_request_changes(args: argparse.Namespace, db_path: str) -> int:
    """Set code_reviews.status = 'changes_requested' and review_pass = 0."""
    diag = _reject_review_note(args.note)
    if diag is not None:
        print(diag, file=sys.stderr)
        return 1

    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, reviewer, status, created_at FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        cost_dollars, tokens_in, tokens_out = _resolve_cost_columns(args, review["created_at"])

        set_clauses = ["status = 'changes_requested'", "review_pass = 0", "updated_at = datetime('now')"]
        params: list = []
        if args.note is not None:
            set_clauses.append("note = ?")
            params.append(args.note or None)
        if args.model is not None:
            set_clauses.append("model = ?")
            params.append(args.model or None)
        if cost_dollars is not None:
            set_clauses.append("cost_dollars = ?")
            params.append(cost_dollars)
        if tokens_in is not None:
            set_clauses.append("tokens_in = ?")
            params.append(tokens_in)
        if tokens_out is not None:
            set_clauses.append("tokens_out = ?")
            params.append(tokens_out)
        params.append(args.review_id)
        conn.execute(
            f"UPDATE code_reviews SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    reviewer_str = f" by {review['reviewer']}" if review["reviewer"] else ""
    if args.note == "":
        note_str = " (note cleared)"
    elif args.note:
        note_str = f" ({args.note})"
    else:
        note_str = ""
    print(f"Review #{args.review_id} changes requested{reviewer_str} for task #{review['task_id']}{note_str}")
    return 0


def cmd_backfill_cost(args: argparse.Namespace, db_path: str) -> int:
    """Recompute cost/tokens columns for an existing review row.

    Two paths:
    - Explicit override — when `--cost-dollars`, `--tokens-in`, and
      `--tokens-out` are all provided, skip transcript auto-compute and
      apply the values directly. Used by /review-commits to attribute
      a spawned reviewer agent's cost to the review row (the orchestrator's
      transcript window doesn't see the agent's API spend).
    - Transcript auto-compute — recompute from the row's `[created_at, now]`
      window. Used to repair rows that finalized without cost data (e.g.
      rows written under an older code path or via `--skip-cost`). If no
      transcript with requests is discoverable, leaves the row unchanged
      and returns 1. Historical pre-v801 NULL rows are out of scope.
    """
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT id, task_id, created_at, cost_dollars, tokens_in, tokens_out"
            " FROM code_reviews WHERE id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        if not getattr(args, "force", False) and review["cost_dollars"] is not None:
            print(
                f"Review #{args.review_id} already has cost_dollars=${review['cost_dollars']:.4f}. "
                "Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1

        explicit_cost = getattr(args, "cost_dollars", None)
        explicit_tin = getattr(args, "tokens_in", None)
        explicit_tout = getattr(args, "tokens_out", None)
        explicit_provided = [x for x in (explicit_cost, explicit_tin, explicit_tout) if x is not None]
        if explicit_provided and len(explicit_provided) < 3:
            print(
                "Error: --cost-dollars, --tokens-in, and --tokens-out must all be provided together "
                "(or omit all three to auto-compute from the transcript window).",
                file=sys.stderr,
            )
            return 2

        if len(explicit_provided) == 3:
            computed = {
                "cost_dollars": explicit_cost,
                "tokens_in": explicit_tin,
                "tokens_out": explicit_tout,
            }
        else:
            computed = _compute_review_cost_from_window(review["created_at"])
            if computed is None:
                print(
                    f"Warning: No transcript with requests in window "
                    f"[{review['created_at']}, now] for review #{args.review_id} — leaving columns unchanged.",
                    file=sys.stderr,
                )
                return 1

        conn.execute(
            "UPDATE code_reviews SET cost_dollars = ?, tokens_in = ?, tokens_out = ?,"
            " updated_at = datetime('now') WHERE id = ?",
            (computed["cost_dollars"], computed["tokens_in"], computed["tokens_out"], args.review_id),
        )
        conn.commit()
    finally:
        conn.close()

    print(
        f"Review #{args.review_id} backfilled: "
        f"cost=${computed['cost_dollars']:.4f}, "
        f"tokens_in={computed['tokens_in']:,}, tokens_out={computed['tokens_out']:,}"
    )
    return 0


def cmd_status(args: argparse.Namespace, db_path: str) -> int:
    """Return JSON with per-reviewer status and comment counts for a task."""
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id, summary FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        reviews = conn.execute(
            "SELECT r.id, r.reviewer, r.status, r.review_pass, r.created_at, r.updated_at,"
            "  COUNT(c.id) as total_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution IS NULL THEN 1 ELSE 0 END) as open_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'fixed' THEN 1 ELSE 0 END) as fixed_comments,"
            "  SUM(CASE WHEN c.id IS NOT NULL AND c.resolution = 'dismissed' THEN 1 ELSE 0 END) as dismissed_comments"
            " FROM code_reviews r"
            " LEFT JOIN review_comments c ON c.review_id = r.id"
            " WHERE r.task_id = ? AND r.status <> 'superseded'"
            " GROUP BY r.id ORDER BY r.id",
            (args.task_id,),
        ).fetchall()
    finally:
        conn.close()

    result = {
        "task_id": args.task_id,
        "task_summary": task["summary"],
        "reviews": [
            {
                "review_id": r["id"],
                "reviewer": r["reviewer"],
                "status": r["status"],
                "review_pass": r["review_pass"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "comment_counts": {
                    "total": r["total_comments"] or 0,
                    "open": r["open_comments"] or 0,
                    "fixed": r["fixed_comments"] or 0,
                    "dismissed": r["dismissed_comments"] or 0,
                },
            }
            for r in reviews
        ],
    }

    print(dumps(result))
    return 0


def cmd_verdict(args: argparse.Namespace, db_path: str) -> int:
    """Return JSON verdict for a task based on open must_fix review comments."""
    conn = get_connection(db_path)
    try:
        task = conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        row = conn.execute(
            "SELECT COUNT(*) as cnt"
            " FROM review_comments rc"
            " JOIN code_reviews cr ON cr.id = rc.review_id"
            " WHERE cr.task_id = ? AND cr.status <> 'superseded'"
            " AND rc.category = 'must_fix' AND rc.resolution IS NULL",
            (args.task_id,),
        ).fetchone()
    finally:
        conn.close()

    open_must_fix = row["cnt"] if row else 0
    verdict = "APPROVED" if open_must_fix == 0 else "CHANGES_REMAINING"
    print(dumps({"verdict": verdict, "open_must_fix": open_must_fix}))
    return 0


def cmd_pass_status(args: argparse.Namespace, db_path: str, config_path: str) -> int:
    """Return JSON with current pass, max passes, can_retry, and open must_fix count."""
    conn = get_connection(db_path)
    try:
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not task:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 2

        pass_row = conn.execute(
            "SELECT review_pass FROM code_reviews"
            " WHERE task_id = ? AND status <> 'superseded' ORDER BY id DESC LIMIT 1",
            (args.task_id,),
        ).fetchone()
        current_pass = pass_row["review_pass"] if pass_row and pass_row["review_pass"] is not None else 0

        must_fix_row = conn.execute(
            "SELECT COUNT(*) as cnt"
            " FROM review_comments rc"
            " JOIN code_reviews cr ON cr.id = rc.review_id"
            " WHERE cr.task_id = ? AND cr.status <> 'superseded'"
            " AND rc.category = 'must_fix' AND rc.resolution IS NULL",
            (args.task_id,),
        ).fetchone()
    finally:
        conn.close()

    open_must_fix = must_fix_row["cnt"] if must_fix_row else 0
    cfg = load_review_config(config_path)
    max_passes = cfg["max_passes"]
    can_retry = current_pass < max_passes and open_must_fix > 0

    print(dumps({
        "current_pass": current_pass,
        "max_passes": max_passes,
        "can_retry": can_retry,
        "open_must_fix": open_must_fix,
    }))
    return 0


def cmd_summary(args: argparse.Namespace, db_path: str) -> int:
    """Output a summary of all findings for a review."""
    conn = get_connection(db_path)
    try:
        review = conn.execute(
            "SELECT r.id, r.task_id, r.reviewer, r.status, r.review_pass,"
            "  r.diff_summary, r.created_at, t.summary as task_summary"
            " FROM code_reviews r JOIN tasks t ON t.id = r.task_id"
            " WHERE r.id = ?",
            (args.review_id,),
        ).fetchone()
        if not review:
            print(f"Error: Review {args.review_id} not found", file=sys.stderr)
            return 2

        comments = conn.execute(
            "SELECT id, file_path, line_start, line_end, category, severity, comment, resolution, resolution_note"
            " FROM review_comments WHERE review_id = ? ORDER BY severity, category, id",
            (args.review_id,),
        ).fetchall()
    finally:
        conn.close()

    reviewer_label = review["reviewer"] or "unassigned"
    verdict = "APPROVED" if review["status"] == "approved" else (
        "CHANGES REQUESTED" if review["status"] == "changes_requested" else review["status"].upper()
    )

    print(f"Review #{review['id']} Summary")
    print(f"Task:     #{review['task_id']} {review['task_summary']}")
    print(f"Reviewer: {reviewer_label}")
    print(f"Status:   {verdict} (pass {review['review_pass']})")
    print(f"Date:     {review['created_at']}")
    if review["diff_summary"]:
        print(f"Diff:     {review['diff_summary']}")
    print()

    if not comments:
        print("No findings.")
        return 0

    open_comments = [c for c in comments if c["resolution"] is None]
    resolved_comments = [c for c in comments if c["resolution"] is not None]

    print(f"Findings: {len(comments)} total, {len(open_comments)} open, {len(resolved_comments)} resolved")
    print()

    if open_comments:
        print("Open findings:")
        for c in open_comments:
            loc = ""
            if c["file_path"]:
                loc = f" {c['file_path']}"
                if c["line_start"]:
                    loc += f":{c['line_start']}"
                    if c["line_end"] and c["line_end"] != c["line_start"]:
                        loc += f"-{c['line_end']}"
            cat = f"[{c['category']}]" if c["category"] else ""
            sev = f"[{c['severity']}]" if c["severity"] else ""
            tags = " ".join(x for x in [cat, sev] if x)
            tags_str = f" {tags}" if tags else ""
            print(f"  #{c['id']}{loc}{tags_str}: {c['comment']}")
        print()

    if resolved_comments:
        print("Resolved findings:")
        for c in resolved_comments:
            loc = ""
            if c["file_path"]:
                loc = f" {c['file_path']}"
                if c["line_start"]:
                    loc += f":{c['line_start']}"
            note = f" — {c['resolution_note']}" if c["resolution_note"] else ""
            print(f"  #{c['id']}{loc} ({c['resolution']}{note}): {c['comment']}")
        print()

    return 0


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk review {start|begin|add-comment|list|resolve|approve|request-changes|status|summary} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk review",
        description="Manage code reviews for tasks",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # start
    start_p = subparsers.add_parser("start", allow_abbrev=False, help="Start a new code review for a task")
    start_p.add_argument("task_id", type=int, help="Task ID")
    start_p.add_argument("--reviewer", default=None, help="Reviewer name (overrides config reviewers)")
    start_p.add_argument("--pass-num", type=int, default=1, help="Review pass number (default: 1)")
    start_p.add_argument("--diff-summary", default=None, help="Optional diff summary text")
    start_p.add_argument("--agent", default=None, help="Agent name that ran the review (e.g. from /chain)")

    # begin
    begin_p = subparsers.add_parser(
        "begin", allow_abbrev=False,
        help="Bundle review-diff-range and review start in one call (returns JSON)",
    )
    begin_p.add_argument("task_id", type=int, help="Task ID")
    begin_p.add_argument("--reviewer", default=None, help="Reviewer name (overrides config reviewers)")
    begin_p.add_argument("--pass-num", type=int, default=1, help="Review pass number (default: 1)")
    begin_p.add_argument("--agent", default=None, help="Agent name that ran the review (e.g. from /chain)")

    # add-comment
    add_comment_p = subparsers.add_parser("add-comment", allow_abbrev=False, help="Add a finding comment to a review")
    add_comment_p.add_argument("review_id", type=int, help="Review ID")
    add_comment_p.add_argument("comment", help="Comment text")
    add_comment_p.add_argument("--file", default=None, help="File path")
    add_comment_p.add_argument("--line-start", type=int, default=None, help="Starting line number")
    add_comment_p.add_argument("--line-end", type=int, default=None, help="Ending line number")
    add_comment_p.add_argument("--category", default=None, help="Finding category (e.g., must_fix, suggest)")
    add_comment_p.add_argument("--severity", default=None, help="Severity (e.g., critical, major, minor)")

    # list
    list_p = subparsers.add_parser("list", allow_abbrev=False, help="List reviews and findings for a task")
    list_p.add_argument("task_id", type=int, help="Task ID")

    # resolve
    resolve_p = subparsers.add_parser("resolve", allow_abbrev=False, help="Resolve a review comment")
    resolve_p.add_argument("comment_id", type=int, help="Comment ID")
    resolve_p.add_argument("resolution", choices=["fixed", "dismissed"], help="Resolution status")
    resolve_p.add_argument(
        "--note",
        default=None,
        help="Optional rationale stored alongside the resolution (e.g. 'Tracked as TASK-42')",
    )

    # approve
    approve_p = subparsers.add_parser("approve", allow_abbrev=False, help="Approve a review")
    approve_p.add_argument("review_id", type=int, help="Review ID")
    approve_p.add_argument(
        "--note",
        help="Optional reason or note to store with the approval. Pass --note '' to clear an existing note.",
    )
    approve_p.add_argument(
        "--model",
        help="Reviewer model ID (e.g. claude-opus-4-7). Pass --model '' to clear an existing model.",
    )
    _add_cost_flags(approve_p)

    # request-changes
    req_changes_p = subparsers.add_parser("request-changes", allow_abbrev=False, help="Request changes on a review")
    req_changes_p.add_argument("review_id", type=int, help="Review ID")
    req_changes_p.add_argument(
        "--note",
        help="Optional reason or note to store with the changes-requested verdict. Pass --note '' to clear an existing note.",
    )
    req_changes_p.add_argument(
        "--model",
        help="Reviewer model ID (e.g. claude-opus-4-7). Pass --model '' to clear an existing model.",
    )
    _add_cost_flags(req_changes_p)

    # backfill-cost
    backfill_cost_p = subparsers.add_parser(
        "backfill-cost", allow_abbrev=False,
        help="Recompute cost/tokens for an existing review row from its created_at window",
    )
    backfill_cost_p.add_argument("review_id", type=int, help="Review ID")
    backfill_cost_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing cost_dollars even if it is already populated",
    )
    backfill_cost_p.add_argument(
        "--cost-dollars",
        dest="cost_dollars",
        type=float,
        default=None,
        help="Explicit cost (USD); requires --tokens-in and --tokens-out. Skips transcript auto-compute.",
    )
    backfill_cost_p.add_argument(
        "--tokens-in",
        dest="tokens_in",
        type=int,
        default=None,
        help="Explicit tokens_in count; requires --cost-dollars and --tokens-out.",
    )
    backfill_cost_p.add_argument(
        "--tokens-out",
        dest="tokens_out",
        type=int,
        default=None,
        help="Explicit tokens_out count; requires --cost-dollars and --tokens-in.",
    )

    # status
    status_p = subparsers.add_parser("status", allow_abbrev=False, help="Show current review status for a task (JSON)")
    status_p.add_argument("task_id", type=int, help="Task ID")

    # summary
    summary_p = subparsers.add_parser("summary", allow_abbrev=False, help="Print a human-readable summary of a review")
    summary_p.add_argument("review_id", type=int, help="Review ID")

    # validate-comments
    validate_p = subparsers.add_parser(
        "validate-comments", allow_abbrev=False,
        help="Dismiss pending review comments whose file_path is not in the diff",
    )
    validate_p.add_argument("review_id", type=int, help="Review ID")

    # verdict
    verdict_p = subparsers.add_parser("verdict", allow_abbrev=False, help="Return JSON verdict for a task (APPROVED or CHANGES_REMAINING)")
    verdict_p.add_argument("task_id", type=int, help="Task ID")

    # pass-status
    pass_status_p = subparsers.add_parser("pass-status", allow_abbrev=False, help="Return JSON with current pass, max passes, can_retry, open_must_fix")
    pass_status_p.add_argument("task_id", type=int, help="Task ID")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "start":
            sys.exit(cmd_start(args, db_path, config_path))
        elif args.command == "begin":
            sys.exit(cmd_begin(args, db_path, config_path))
        elif args.command == "add-comment":
            sys.exit(cmd_add_comment(args, db_path, config_path))
        elif args.command == "list":
            sys.exit(cmd_list(args, db_path))
        elif args.command == "resolve":
            sys.exit(cmd_resolve(args, db_path))
        elif args.command == "validate-comments":
            sys.exit(cmd_validate_comments(args, db_path))
        elif args.command == "approve":
            sys.exit(cmd_approve(args, db_path))
        elif args.command == "request-changes":
            sys.exit(cmd_request_changes(args, db_path))
        elif args.command == "backfill-cost":
            sys.exit(cmd_backfill_cost(args, db_path))
        elif args.command == "status":
            sys.exit(cmd_status(args, db_path))
        elif args.command == "summary":
            sys.exit(cmd_summary(args, db_path))
        elif args.command == "verdict":
            sys.exit(cmd_verdict(args, db_path))
        elif args.command == "pass-status":
            sys.exit(cmd_pass_status(args, db_path, config_path))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
