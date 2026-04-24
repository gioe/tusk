"""Shared git helpers for tusk scripts.

Originally extracted from tusk-branch.py and tusk-merge.py to prevent drift
of the unreachable-remote detection patterns. Now also the single source of
truth for the `--grep=[TASK-<id>]` escape policy (see issue #537 / TASK-150):
every caller that walks git history for a task's commits routes through
`task_grep_arg()` / `find_task_commits()` instead of building the grep
pattern inline, so the POSIX BRE bracket-escape bug fixed in TASK-149
cannot recur.

The `_has_remote` wrapper itself is left in each caller so that it uses
each script's module-local ``run`` (which tests patch to stub subprocess
calls).

Loaded via tusk_loader:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _git_helpers = tusk_loader.load("tusk-git-helpers")
    _is_remote_unreachable = _git_helpers._is_remote_unreachable
    task_grep_arg = _git_helpers.task_grep_arg
    find_task_commits = _git_helpers.find_task_commits
"""

import re
import subprocess


_UNREACHABLE_REMOTE_PATTERNS = (
    "unable to access",
    "could not resolve host",
    "could not read from remote repository",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "repository not found",
    "does not appear to be a git repository",
    "temporary failure in name resolution",
    "name or service not known",
    "no route to host",
)

# git sometimes inlines the failing URL: `fatal: repository 'https://…' not found`.
_UNREACHABLE_REMOTE_REGEX = re.compile(r"repository '[^']*' not found", re.IGNORECASE)


def _is_remote_unreachable(stderr: str) -> bool:
    """Return True if *stderr* indicates the remote is unreachable rather than
    a local merge problem. Used to distinguish network/DNS/404 failures (where
    we can safely fall back to local state) from divergent-history or merge
    conflicts (where we must hard-fail)."""
    lower = stderr.lower()
    if any(pat in lower for pat in _UNREACHABLE_REMOTE_PATTERNS):
        return True
    return bool(_UNREACHABLE_REMOTE_REGEX.search(stderr))


def task_grep_arg(task_id: int) -> str:
    r"""Return a ``--grep=\[TASK-<id>\]`` argument with brackets escaped for git BRE.

    Git's ``--grep`` uses POSIX BRE: an unescaped ``[TASK-<id>]`` is parsed as
    a character class with the reversed range ``K-1`` (K=75, 1=49), which git
    rejects with ``invalid character range`` — emptying the result for every
    task ID. Centralizing the escape here removes a recurring-bug surface
    (see issue #534 / TASK-149, issue #537 / TASK-150).
    """
    return rf"--grep=\[TASK-{task_id}\]"


def find_task_commits(
    task_id: int,
    repo_root: str,
    refs: list | None = None,
    since: str | None = None,
) -> list:
    r"""Return commit SHAs referencing ``[TASK-<id>]`` across the given refs.

    ``refs`` lists extra ref-args to pass to ``git log`` (e.g. ``["--all"]``,
    ``["main"]``, or ``["<branch>", "--not", "<default>"]``). If None or
    empty, git log defaults to HEAD. ``since`` is forwarded as
    ``--since=<since> UTC`` so SQLite-stored UTC timestamps anchor correctly
    against git's local-time interpretation of ``--since``.

    Returns ``[]`` on non-zero exit (callers cannot distinguish "no commits"
    from "git errored"; every existing call site treats both the same).
    """
    args = ["git", "log"]
    if refs:
        args.extend(refs)
    args.extend(["--format=%H", task_grep_arg(task_id)])
    if since:
        args.append(f"--since={since} UTC")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
