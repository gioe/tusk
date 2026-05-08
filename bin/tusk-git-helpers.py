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

Also hosts the shared "did this commit touch files this task is about?"
helpers used by the prefix-collision file-overlap heuristic in
tusk-check-deliverables.py and tusk-task-unstart.py (see issue #627).

Loaded via tusk_loader:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _git_helpers = tusk_loader.load("tusk-git-helpers")
    _is_remote_unreachable = _git_helpers._is_remote_unreachable
    task_grep_arg = _git_helpers.task_grep_arg
    find_task_commits = _git_helpers.find_task_commits
    extract_paths = _git_helpers.extract_paths
    default_branch = _git_helpers.default_branch
    commit_changed_files = _git_helpers.commit_changed_files
    task_referenced_paths = _git_helpers.task_referenced_paths
    iter_branch_auto_stashes = _git_helpers.iter_branch_auto_stashes
"""

import os
import re
import sqlite3
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

_BRANCH_AUTOSTASH_LINE_RE = re.compile(
    r"^stash@\{(\d+)\}: .*: tusk-branch: auto-stash for TASK-(\d+)$"
)


def _is_remote_unreachable(stderr: str) -> bool:
    """Return True if *stderr* indicates the remote is unreachable rather than
    a local merge problem. Used to distinguish network/DNS/404 failures (where
    we can safely fall back to local state) from divergent-history or merge
    conflicts (where we must hard-fail)."""
    lower = stderr.lower()
    if any(pat in lower for pat in _UNREACHABLE_REMOTE_PATTERNS):
        return True
    return bool(_UNREACHABLE_REMOTE_REGEX.search(stderr))


def iter_branch_auto_stashes(repo_root: str | None = None, runner=None):
    """Yield ``(stash_index, task_id)`` for ``tusk-branch`` auto-stash entries.

    Uses ``refs/stash`` as a cheap fast-exit before listing stashes, and
    matches the full line through ``TASK-N`` so ``TASK-2`` does not collide with
    ``TASK-29``.
    """
    if runner is None:
        def run_git(args):
            kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
            }
            if repo_root is not None:
                kwargs["cwd"] = repo_root
            return subprocess.run(args, **kwargs)
    else:
        def run_git(args):
            return runner(args, check=False)

    if run_git(["git", "rev-parse", "--verify", "--quiet", "refs/stash"]).returncode != 0:
        return

    stash_list = run_git(["git", "stash", "list"])
    if stash_list.returncode != 0:
        return

    for line in stash_list.stdout.splitlines():
        match = _BRANCH_AUTOSTASH_LINE_RE.match(line.rstrip())
        if not match:
            continue
        try:
            yield (int(match.group(1)), int(match.group(2)))
        except ValueError:
            pass


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


# ── Prefix-collision file-overlap heuristic ───────────────────────────
#
# Originally embedded in tusk-check-deliverables.py. Hoisted here so
# tusk-task-unstart.py (and any future caller) can apply the same
# "do these [TASK-<id>] commits actually touch files this task is about?"
# check without reimplementing it. See issue #627.

# Common bare top-level deliverable files. These are matched by name when they
# appear unprefixed in free-form text — without this whitelist, _PATH_RE's
# directory-prefix requirement would silently drop them and downstream callers
# (task-summary's file-overlap heuristic, check-deliverables, task-unstart)
# would then mis-attribute commits whose only changed paths are these files.
# See issues #661 (tusk-internal: CLAUDE.md/AGENTS.md/VERSION/README.md/CHANGELOG.md)
# and #662 (non-tusk projects: Python/Docker/build/metadata files).
_BARE_TOPLEVEL_WHITELIST = frozenset({
    # tusk-internal deliverables
    "CLAUDE.md",
    "AGENTS.md",
    "VERSION",
    "README.md",
    "CHANGELOG.md",
    # Python project deliverables
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "tox.ini",
    "MANIFEST.in",
    # Docker / container deliverables
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    # Build tooling
    "Makefile",
    "Justfile",
    "Rakefile",
    # Repo metadata
    "LICENSE",
    "NOTICE",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
})

_BARE_TOPLEVEL_ALTERNATION = "|".join(re.escape(n) for n in _BARE_TOPLEVEL_WHITELIST)

# Regex to extract candidate file paths from unstructured text.
# First alternative: tokens that start with a path-like prefix and contain at
# least one dot (suggesting a filename with an extension).
# Second alternative: bare top-level whitelisted filenames, with a negative
# lookahead on a trailing word char so VERSIONS / README.markdown don't match
# the VERSION / README.md prefixes.
_PATH_RE = re.compile(
    r'(?:^|[\s\'"`(,])('
    r'(?:'
    r'(?:\./|\.\./|\.claude/|\.claude\\|bin/|skills[-_]?internal/|skills/|tests?/|docs?/|src/'
    r'|(?!\w+://)\w[\w._-]*/'  # any directory prefix that is not a URL protocol
    r')'
    r'[\w./_-]+'
    r'|'
    r'(?:' + _BARE_TOPLEVEL_ALTERNATION + r')(?!\w)'
    r')'
    r')',
    re.MULTILINE,
)


def extract_paths(text: str) -> list:
    """Extract candidate file paths from free-form text."""
    if not text:
        return []
    paths = []
    for m in _PATH_RE.finditer(text):
        p = m.group(1).strip().rstrip('.,;:\'"`)')
        if not p or '://' in p:
            continue
        # Whitelisted bare top-level files bypass the extension check
        # (e.g. VERSION has no extension); everything else must have a dot
        # in the basename so we don't chase bare directory names.
        if p in _BARE_TOPLEVEL_WHITELIST or '.' in os.path.basename(p):
            paths.append(p)
    return paths


# Regex to extract bare-basename file-like tokens (basename with multi-char
# extension) that did NOT match _PATH_RE — i.e. no directory prefix and not
# in the whitelist. The extension must be at least 2 chars to exclude
# sentence-ending tokens like "e.g." and "i.e." (issue #670).
#
# Used by tusk-task-summary.py's block-level scope filter to resolve
# bare-basename references (e.g. "FULL-RETRO.md") to commit-touched paths
# (e.g. "skills/retro/FULL-RETRO.md") at *basename* match level rather
# than full-path level. The strict full-path filter misses these because
# the description never names the directory; matching on basename keeps
# the legitimate work in scope without admitting unrelated work, since
# the candidate pool is already restricted to [TASK-N]-grep-matched
# commits before this filter runs.
_BARE_BASENAME_RE = re.compile(
    r'(?:^|[\s\'"`(,])'
    r'([A-Za-z][\w.-]*\.[A-Za-z][\w]{1,9})'
    r'(?=[\s\'"`),.;:]|$)',
    re.MULTILINE,
)


def extract_referenced_basenames(text: str) -> list:
    """Bare basenames (file-like tokens with multi-char extension) not
    already covered by extract_paths. Companion to extract_paths used
    by the block-level scope filter in tusk-task-summary.py (issue #670).
    """
    if not text:
        return []
    already = set(extract_paths(text))
    bare = []
    seen: set = set()
    for m in _BARE_BASENAME_RE.finditer(text):
        name = m.group(1).strip().rstrip('.,;:\'"`)')
        if not name or '://' in name:
            continue
        if name in already or name in _BARE_TOPLEVEL_WHITELIST:
            continue
        if name not in seen:
            seen.add(name)
            bare.append(name)
    return bare


def default_branch(repo_root: str) -> str:
    """Detect the default branch: symbolic-ref → gh fallback → 'main'.

    Mirrors cmd_git_default_branch in bin/tusk.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().removeprefix("refs/remotes/origin/")
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def commit_changed_files(commits: list, repo_root: str) -> set:
    """Return the union of changed file paths across the given commits."""
    files: set = set()
    for sha in commits:
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(line)
    return files


def task_referenced_paths(task_id: int, conn: sqlite3.Connection) -> list:
    """Return paths referenced in task summary/description/criteria/specs (no existence check)."""
    row = conn.execute(
        "SELECT summary, description FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return []

    criteria_rows = conn.execute(
        "SELECT criterion, verification_spec FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchall()

    texts = [row["summary"] or "", row["description"] or ""]
    for cr in criteria_rows:
        texts.append(cr["criterion"] or "")
        texts.append(cr["verification_spec"] or "")

    candidates = []
    seen: set = set()
    for text in texts:
        for p in extract_paths(text):
            if p not in seen:
                seen.add(p)
                candidates.append(p)
    return candidates


def task_referenced_basenames(task_id: int, conn: sqlite3.Connection) -> list:
    """Bare basenames referenced in summary/description/criteria/specs.

    Companion to task_referenced_paths — returns tokens that look like
    filenames but lack a directory prefix and aren't in the whitelist.
    Used by tusk-task-summary.py for basename-level scope matching
    (issue #670). No existence check.
    """
    row = conn.execute(
        "SELECT summary, description FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return []

    criteria_rows = conn.execute(
        "SELECT criterion, verification_spec FROM acceptance_criteria WHERE task_id = ?",
        (task_id,),
    ).fetchall()

    texts = [row["summary"] or "", row["description"] or ""]
    for cr in criteria_rows:
        texts.append(cr["criterion"] or "")
        texts.append(cr["verification_spec"] or "")

    candidates = []
    seen: set = set()
    for text in texts:
        for name in extract_referenced_basenames(text):
            if name not in seen:
                seen.add(name)
                candidates.append(name)
    return candidates
