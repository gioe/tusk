#!/usr/bin/env python3
"""Worktree-pool health insights.

Surfaces three counts so worktree accumulation stops being silent:
- ``reconcile_eligible`` — registry rows whose task is Done and whose
  workspace_path is still on disk (would be cleaned up by
  ``tusk task-worktree reconcile``).
- ``prune_eligible`` — registry rows whose workspace_path is gone AND
  which are not in ``git worktree list`` (would be removed by
  ``tusk task-worktree prune`` — same staleness predicate as
  ``_is_stale_workspace`` in ``tusk-task-worktree.py``).
- ``disk_usage_bytes`` — sum of file sizes under each existing
  workspace_path in the registry. The registry is per-DB so this is
  already scoped to the current repo.

Invocation:
    python3 bin/tusk-insights.py <db_path> <config_path> <repo_root>
                                 [--format json|text]
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
get_connection = _db_lib.get_connection
dumps = _json_lib.dumps


def _parse_git_worktrees(repo_root: str) -> dict:
    """Return ``{branch: workspace_path}`` from ``git worktree list``.

    Mirrors the helper in ``tusk-task-worktree.py`` so the prune-eligible
    classification stays in lockstep with the actual prune subcommand.
    """
    result = subprocess.run(
        ["git", "-C", repo_root, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return {}
    by_branch: dict[str, str] = {}
    current_path: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/") and current_path:
            branch = line[len("branch refs/heads/"):].strip()
            by_branch[branch] = current_path
    return by_branch


def _dir_size_bytes(path: str) -> int:
    """Sum file sizes under ``path``, swallowing per-file OS errors.

    ``followlinks=False`` keeps the walk inside the worktree even when
    callers symlink in ``node_modules`` / ``.venv`` (canonical-fallback
    behavior from issue #854) — otherwise size would balloon by the
    primary's runtime artifacts on every reconcile candidate.
    """
    total = 0
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                total += os.lstat(full).st_size
            except OSError:
                continue
    return total


def collect_health(db_path: str, repo_root: str) -> dict:
    conn = get_connection(db_path)
    try:
        live_by_branch = _parse_git_worktrees(repo_root)
        rows = conn.execute(
            """
            SELECT tw.id           AS workspace_id,
                   tw.task_id      AS task_id,
                   tw.branch       AS branch,
                   tw.workspace_path AS workspace_path,
                   t.status        AS task_status
            FROM task_workspaces tw
            LEFT JOIN tasks t ON t.id = tw.task_id
            ORDER BY tw.id
            """
        ).fetchall()

        reconcile_eligible = 0
        prune_eligible = 0
        disk_usage_bytes = 0
        for row in rows:
            workspace_path = row["workspace_path"]
            exists = os.path.isdir(workspace_path)
            in_live = row["branch"] in live_by_branch
            if exists:
                disk_usage_bytes += _dir_size_bytes(workspace_path)
                if row["task_status"] == "Done":
                    reconcile_eligible += 1
            elif not in_live:
                prune_eligible += 1

        suggestions: list[str] = []
        if reconcile_eligible:
            suggestions.append(
                f"Run `tusk task-worktree reconcile` to clean up "
                f"{reconcile_eligible} reconcile-eligible row(s)."
            )
        if prune_eligible:
            suggestions.append(
                f"Run `tusk task-worktree prune` for {prune_eligible} "
                "orphaned row(s)."
            )

        return {
            "reconcile_eligible": reconcile_eligible,
            "prune_eligible": prune_eligible,
            "disk_usage_bytes": disk_usage_bytes,
            "suggestions": suggestions,
        }
    finally:
        conn.close()


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def render_text(report: dict) -> str:
    lines = [
        "### Worktree Pool Health",
        f"- Reconcile-eligible rows: {report['reconcile_eligible']}",
        f"- Prune-eligible rows:     {report['prune_eligible']}",
        f"- Total disk usage:        "
        f"{_format_bytes(report['disk_usage_bytes'])} "
        f"({report['disk_usage_bytes']} bytes)",
    ]
    if report["suggestions"]:
        for suggestion in report["suggestions"]:
            lines.append(f"- {suggestion}")
    else:
        lines.append("- No worktree accumulation detected.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk insights",
        description=(
            "Worktree-pool health: reconcile-eligible rows, "
            "prune-eligible rows, total disk usage."
        ),
    )
    parser.add_argument("db_path")
    parser.add_argument("config_path")
    parser.add_argument("repo_root")
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv)

    report = collect_health(args.db_path, args.repo_root)
    if args.format == "json":
        print(dumps(report))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
