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
import posixpath
import re
import sqlite3
import subprocess
import sys


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


# ── Three-layer recovery for [TASK-N] commit lookups (issue #848) ──────
#
# Both tusk-task-summary's fetch_diff and tusk-task-done's auto-mark walk
# git history for [TASK-<id>] commits. Before issue #848 the two paths had
# divergent recovery: task-summary layered (1) the plain --all scan,
# (2) a best-effort `git fetch origin <default>` retry, and (3) an
# `_unreachable_task_commits` fsck-based scan; task-done had only (1).
# A `tusk task-done --reason completed` against a task whose commits only
# lived in the local object store (no-checkout fast-forward push + broken
# remote URL) failed to auto-mark criteria and exited 3.
#
# The two SHA-finding building blocks below — `try_fetch_default_branch`
# and `find_unreachable_task_commits` — are the shared primitives, and
# `find_task_commits_with_recovery` orchestrates the (initial → fetch+retry
# → fsck) chain for SHA-only callers. task-summary preserves its
# numstat-fetching path (criterion-hash fallback included); task-done
# routes its auto-mark commit lookup through the recovery-aware helper.


def try_fetch_default_branch(repo_root: str) -> None:
    """Best-effort ``git fetch origin <default>`` to refresh ``refs/remotes/origin/<default>``.

    Used by recovery paths when the initial ``git log --all --grep`` scan
    returns nothing: after a no-checkout fast-forward push (``tusk merge``
    from a sibling worktree while the default branch is locked in the
    primary), the local feature branch is deleted and the local default
    branch never advances. The remote-tracking ref ``refs/remotes/origin/<default>``
    SHOULD have been advanced by the push, but several real-world
    environments report the summarizing checkout still seeing the pre-push
    tip — collapsing diff stats to ``0 commits / 0 files`` even though the
    commits exist on origin.

    Silent on failure: a missing remote, network outage, or unknown default
    branch must not abort the recovery — callers just continue with what
    they already have. A 10s timeout guards against a hanging remote.
    """
    try:
        default = default_branch(repo_root)
    except Exception:
        return
    try:
        subprocess.run(
            ["git", "fetch", "origin", default],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def find_unreachable_task_commits(task_id: int, repo_root: str) -> list:
    r"""Return SHAs of unreachable ``[TASK-<id>]`` commits in the local object store.

    Last-resort recovery (issue #845, generalized in issue #848): catches the
    post-no-checkout-push state where every ref-based lookup has come up empty:
      * ``refs/remotes/origin/<default>`` was never advanced (or was deleted)
      * the best-effort ``git fetch`` retry failed silently (broken remote
        URL, no network, no remote configured)

    The commit object is still in the local object store: ``tusk merge``'s
    no-checkout fast-forward push deposits it before removing the sibling
    worktree, and ``git worktree remove`` does NOT prune objects from the
    shared ``.git/objects`` directory. We just need to scan unreachable
    objects and filter by the ``[TASK-<id>]`` commit-message prefix.

    Gated on the prior layers producing nothing — ``git fsck`` walks the
    full object store and is O(objects), so paying this cost on the common
    path would penalize every well-merged task. Silent on every error:
    fsck failures, no candidates, and grep failures all return ``[]`` so
    callers continue with empty results rather than aborting.
    """
    try:
        fsck = subprocess.run(
            ["git", "fsck", "--unreachable", "--no-reflogs"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if fsck.returncode != 0:
        return []

    candidates = [
        parts[2]
        for parts in (line.split() for line in fsck.stdout.splitlines())
        if len(parts) >= 3 and parts[0] == "unreachable" and parts[1] == "commit"
    ]
    if not candidates:
        return []

    # Single ``git log --no-walk`` filters candidates by the [TASK-<id>] grep
    # without spawning a subprocess per SHA. ``task_grep_arg`` returns a BRE
    # pattern (brackets escaped), so do NOT pass ``--fixed-strings``.
    try:
        filter_res = subprocess.run(
            ["git", "log", "--no-walk", task_grep_arg(task_id), "--format=%H"]
            + candidates,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if filter_res.returncode != 0:
        return []

    return [line.strip() for line in filter_res.stdout.splitlines() if line.strip()]


def find_task_commits_with_recovery(
    task_id: int,
    repo_root: str,
    refs: list | None = None,
    since: str | None = None,
) -> tuple:
    r"""Return ``(commits, recovered_via)`` from a layered SHA lookup.

    Layered recovery (issue #848) for callers that need SHAs but not numstat
    data (``tusk task-done``'s auto-mark, future SHA-only consumers):

      1. ``find_task_commits(task_id, repo_root, refs, since)`` — the
         standard ``git log --grep`` scan.
      2. If empty: ``try_fetch_default_branch(repo_root)`` to refresh
         ``refs/remotes/origin/<default>``, then retry step 1.
      3. If still empty: ``find_unreachable_task_commits(task_id, repo_root)``
         (scans the local object store via ``git fsck``).

    ``refs`` defaults to ``["--all"]`` so the recovery layers can actually
    see commits that aren't reachable from HEAD (the canonical no-checkout
    fast-forward push scenario). Callers that want HEAD-only semantics pass
    ``refs=["HEAD"]`` explicitly.

    ``recovered_via`` is ``None`` on the cheap (step 1) path, ``"refresh-fetch"``
    if step 2 produced the result, and ``"fsck-unreachable"`` if step 3 did.
    Callers may use this to emit a diagnostic, but the field is informational
    only — behavior is identical regardless.

    Mirrors ``tusk-task-summary``'s layering except for the criterion-hash
    tier, which is only meaningful when the criteria are already closed
    (``commit_hash`` populated) — irrelevant to task-done's auto-mark, which
    runs against open criteria. Returns ``([], None)`` if every layer comes
    up empty.
    """
    if refs is None:
        refs = ["--all"]

    commits = find_task_commits(task_id, repo_root, refs=refs, since=since)
    if commits:
        return commits, None

    try_fetch_default_branch(repo_root)
    commits = find_task_commits(task_id, repo_root, refs=refs, since=since)
    if commits:
        return commits, "refresh-fetch"

    commits = find_unreachable_task_commits(task_id, repo_root)
    if commits:
        return commits, "fsck-unreachable"

    return [], None


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
    "config.default.json",
    "config.json",
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
_EXTENSIONLESS_SCRIPT_PREFIXES = ("bin/", "hooks/git/", "scripts/")

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
    r'|\.[A-Za-z0-9][\w._-]*/'
    r'|(?!\w+://)\w[\w._-]*/'  # any directory prefix that is not a URL protocol
    r')'
    # "@" admits literal @ path segments (apps/web/@/components/..., issue
    # #1047) alongside the bracket segments ([id]) from issue #1030.
    r'[\w./_\-\[\]@]+'
    r'|'
    r'(?:' + _BARE_TOPLEVEL_ALTERNATION + r')(?!\w)'
    r')'
    r')',
    re.MULTILINE,
)

_LEADING_CD_RE = re.compile(
    r"^\s*cd\s+(?:'([^']+)'|\"([^\"]+)\"|([^\s;&|]+))\s*(?:&&|;)"
)


def _leading_cd_dir(text: str) -> str | None:
    """Return a simple leading ``cd <dir> &&`` / ``cd <dir>;`` target."""
    m = _LEADING_CD_RE.match(text or "")
    if not m:
        return None
    cd_dir = next((g for g in m.groups() if g), "")
    cd_dir = cd_dir.strip().strip("/")
    if not cd_dir or cd_dir.startswith(("-", "/", "../")) or "/../" in cd_dir:
        return None
    return cd_dir


def _prefix_cd_relative_path(path: str, cd_dir: str | None) -> str:
    if not cd_dir or not path or path in _BARE_TOPLEVEL_WHITELIST:
        return path
    if path.startswith(("./", "../", "/")) or path == cd_dir or path.startswith(f"{cd_dir}/"):
        return path
    if "/" not in path:
        return path
    return posixpath.normpath(f"{cd_dir}/{path}")


def extract_paths(text: str) -> list:
    """Extract candidate file paths from free-form text."""
    if not text:
        return []
    cd_dir = _leading_cd_dir(text)
    paths = []
    for m in _PATH_RE.finditer(text):
        p = m.group(1).strip().rstrip('.,;:\'"`)')
        if not p or '://' in p:
            continue
        if "/" in p:
            first, rest = p.split("/", 1)
            if first in _BARE_TOPLEVEL_WHITELIST and "/" not in rest:
                paths.append(first)
                if rest in _BARE_TOPLEVEL_WHITELIST or "." in os.path.basename(rest):
                    paths.append(rest)
                continue
        # Whitelisted bare top-level files bypass the extension check
        # (e.g. VERSION has no extension); everything else must have a dot
        # in the basename unless it is an extensionless script under a known
        # executable-script prefix.
        basename = os.path.basename(p.rstrip("/"))
        is_extensionless_script = (
            basename
            and not p.endswith("/")
            and "." not in basename
            and any(p.startswith(prefix) for prefix in _EXTENSIONLESS_SCRIPT_PREFIXES)
        )
        is_github_directory_scope = (
            basename
            and p.endswith("/")
            and p.startswith(".github/")
        )
        if (
            p in _BARE_TOPLEVEL_WHITELIST
            or "." in basename
            or is_extensionless_script
            or is_github_directory_scope
        ):
            paths.append(_prefix_cd_relative_path(p, cd_dir))
    return paths


def path_exists_in_repo(repo_root: str | None, path: str | None) -> bool:
    """Return True when ``path`` resolves to a real repo-root entry.

    The free-form path extractor intentionally accepts broad path-shaped
    tokens. Scope declarations and sparse-checkout cones need a stricter
    filter so prose such as ``console.error/console.log`` is not treated as a
    repository path.
    """
    if not repo_root or not path:
        return False
    raw = path.strip()
    if (
        not raw
        or raw.startswith(("./", "../", "/"))
    ):
        return False
    candidate = raw.strip("/")
    if not candidate:
        return False
    parts = candidate.split("/")
    if any(seg in {"", ".."} for seg in parts):
        return False

    on_disk = os.path.join(repo_root, candidate)
    if os.path.exists(on_disk):
        return True

    result = subprocess.run(
        ["git", "-C", repo_root, "ls-tree", "--name-only", "HEAD", "--", candidate],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return False
    return any(
        line.strip().rstrip("/") == candidate
        for line in result.stdout.splitlines()
    )


_FILE_SPEC_GLOB_METACHARS = "*?["


def file_spec_glob_metachars(spec: str | None) -> list:
    """Return the glob metacharacters present in a file-type verification spec.

    File-type criteria are verified with glob.glob, so a bracket sequence
    like ``[wght]`` is a character class, not literal text. Literal filenames
    containing brackets are real (Google Fonts ships variable fonts named
    ``Chivo[wght].ttf``), so callers warn rather than reject (issue #1032).
    """
    if not spec:
        return []
    return [ch for ch in _FILE_SPEC_GLOB_METACHARS if ch in spec]


def warn_file_spec_glob_metachars(spec: str | None, source: str) -> bool:
    """Warn on stderr when a file-type spec contains glob metacharacters.

    Returns True when a warning was printed. Deliberately distinct from the
    path-does-not-exist warning: that one is expected for file-type criteria
    whose deliverable is created later, so it reads as benign; this one names
    the offending metacharacters and the glob-vs-literal mismatch.
    """
    chars = file_spec_glob_metachars(spec)
    if not chars:
        return False
    joined = " ".join(repr(ch) for ch in chars)
    print(
        f"Warning: {source} file-type verification_spec contains glob "
        f"metacharacter(s) {joined} — the spec is matched as a glob at "
        f"verification time, not as a literal path. Escape '[' as '[[]' "
        f"if a literal filename is intended: {spec}",
        file=sys.stderr,
    )
    return True


def is_prose_identifier_path(path: str | None, repo_root: str | None = None) -> bool:
    """Return True for slash-joined code identifiers masquerading as paths."""
    if not path or "/" not in path:
        return False
    raw = path.strip()
    if path_exists_in_repo(repo_root, raw):
        return False
    parts = raw.split("/")
    first = path.strip().split("/", 1)[0]
    # A dot-prefixed segment ANYWHERE in the path signals a hidden/runtime
    # directory crossing mid-path (e.g. ``node_modules/.venv``,
    # ``foo/.git/bar``) — a strong prose-concatenation signal. The original
    # rule only checked the first segment, so symmetric junk such as
    # ``node_modules/.venv`` (first segment is a plain name, the ``.venv``
    # is second) slipped through and landed as a bogus auto_derived scope
    # row (issue #1093). Real source paths whose dot-prefixed segments
    # actually exist are already returned above via path_exists_in_repo;
    # callers that legitimately want non-existent ``.github/...`` paths
    # (e.g. task-insert's explicit-github carve-out) gate this call
    # accordingly.
    if any(seg.startswith(".") for seg in parts):
        return True
    if "." in first:
        return True
    return any(re.fullmatch(r"\d+(?:\.\d+)+", part) for part in parts[1:])


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


def commit_parents_map(shas: list, repo_root: str) -> dict:
    """Return ``{sha: [parent_shas]}`` for the given commit SHAs.

    A single ``git log --no-walk --format='%H %P'`` call covers the whole
    set so block-grouping callers stay at one subprocess regardless of
    commit count. Missing SHAs come back with an empty parent list.
    """
    if not shas:
        return {}
    result = subprocess.run(
        ["git", "log", "--no-walk", "--format=%H %P", *shas],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    parents: dict = {sha: [] for sha in shas}
    if result.returncode != 0:
        return parents
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if parts:
            parents[parts[0]] = parts[1:]
    return parents


def filter_commits_by_block_overlap(
    commits: list,
    task_id: int,
    repo_root: str,
    conn: sqlite3.Connection | None,
    *,
    commit_files: dict | None = None,
    commit_parents: dict | None = None,
    task_paths: set | None = None,
    task_basenames: set | None = None,
    fallthrough: bool = True,
) -> list:
    """Drop commits whose connected-component block doesn't overlap this task's scope signal.

    Centralized block-level scope filter (issue #855). Originally fanned out
    across review-diff-range, task-summary, and task-done (TASK-308/309 for
    issue #656); the heuristic drifted three times in ~weeks (TASK-309 →
    TASK-433 → TASK-434) before being hoisted here.

    Groups *commits* into connected components on the parent chain, then
    keeps each block whose aggregate file paths intersect
    ``task_referenced_paths`` (full path) OR whose basenames intersect
    ``task_referenced_basenames`` (issue #670). Extraction-miss fallthrough
    (issue #851): when zero blocks intersect the scope signal, returns
    *commits* unchanged — the signal is more likely off-scope (a precedent
    citation in the description) than every matched commit being a
    recycled-ID stray. Over-inclusion is recoverable; silent zero-range
    refusal is not.

    Returns *commits* unchanged when:
      - *commits* is empty
      - *conn* is ``None`` (caller signaling no DB available)
      - the task has no scope signal (no referenced paths and no basenames)
      - the extraction-miss fallthrough fires (no block intersects) AND
        *fallthrough* is True (the filter-caller default)

    Optional inputs let callers reuse precomputed values to avoid redundant
    git subprocess calls or DB queries:
      - *commit_files* — ``{sha: set(paths)}`` per-commit changed-file sets.
        Falls back to ``commit_changed_files([sha], repo_root)`` per block.
      - *commit_parents* — ``{sha: [parent_shas]}`` parent map. Falls back
        to a single ``commit_parents_map(commits, repo_root)`` call.
      - *task_paths* — pre-resolved scope-signal full-path set. Skips the
        in-helper ``task_referenced_paths`` DB query when provided. Pass
        an empty set to actively suppress the path leg of the match.
      - *task_basenames* — pre-resolved scope-signal basename set. Same
        contract as *task_paths*. Gate callers that want to preserve the
        legacy path-only behavior pass ``set()`` here.
      - *fallthrough* — when False, the "no block intersects" path returns
        ``[]`` instead of *commits* unchanged. Gate callers (tusk-merge,
        tusk-task-unstart) opt in so they can distinguish "every block is
        off-scope" (treat as prefix-match false positive, override the
        gate) from "no scope signal to discriminate" (preserve the gate's
        refusal). Filter callers keep the default so extraction misses
        don't silently collapse a real-work diff to empty (issue #855).

    Returns kept SHAs in their original order in *commits*.
    """
    if not commits or conn is None:
        return list(commits)
    if task_paths is None:
        task_paths = set(task_referenced_paths(task_id, conn))
    if task_basenames is None:
        task_basenames = set(task_referenced_basenames(task_id, conn))
    if not task_paths and not task_basenames:
        return list(commits)

    matched = set(commits)
    if commit_parents is None:
        commit_parents = commit_parents_map(commits, repo_root)

    uf: dict = {sha: sha for sha in matched}

    def find(x: str) -> str:
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            uf[ra] = rb

    for sha, parents in commit_parents.items():
        if sha not in matched:
            continue
        for p in parents:
            if p in matched:
                union(sha, p)

    blocks: dict = {}
    for sha in matched:
        blocks.setdefault(find(sha), []).append(sha)

    kept: set = set()
    for block_shas in blocks.values():
        if commit_files is not None:
            block_files: set = set()
            for sha in block_shas:
                block_files |= commit_files.get(sha, set())
        else:
            block_files = commit_changed_files(block_shas, repo_root)
        block_basenames = {os.path.basename(p) for p in block_files}
        if (block_files & task_paths) or (block_basenames & task_basenames):
            kept.update(block_shas)

    if not kept:
        if fallthrough:
            return list(commits)
        return []

    return [sha for sha in commits if sha in kept]


# ── Auto-generated lockfiles ──────────────────────────────────────────
#
# Lockfiles regenerated by package managers and not hand-authored. Used by:
#   - tusk-merge.py — auto-resolve stash-pop conflicts in lockfiles by
#     preferring the WIP version.
#   - tusk-review-diff-range.py — subtract lockfile-section lines from the
#     diff_lines_meaningful count so the /review-commits routing threshold
#     tracks the size of human-readable changes (issue #761).
#
# Matched by basename, so the same path inside any subdirectory counts.

GENERATED_LOCKFILES = frozenset([
    "Package.resolved",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Podfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "poetry.lock",
    "flake.lock",
    "mix.lock",
    "go.sum",
    "pubspec.lock",
    "Pipfile.lock",
    "pdm.lock",
    "uv.lock",
])


def filter_lockfile_diff_sections(diff_out: str) -> str:
    """Return *diff_out* with every per-file section for an auto-generated
    lockfile removed (header line included).

    A ``git diff`` payload is a concatenation of per-file sections, each
    introduced by ``diff --git a/<path> b/<path>``. This function walks the
    output line-by-line, identifies the ``b/<path>`` of each section's
    introducing header, and drops the section entirely when its basename is
    in :data:`GENERATED_LOCKFILES`.

    Binary-file diffs (``Binary files a/foo and b/bar differ``) and rename
    headers (where the ``diff --git`` line still names the new path under
    ``b/``) are handled by the same parse — anything between two ``diff --git``
    lines belongs to the preceding header's file.

    Used to compute ``diff_lines_meaningful`` (issue #761) without altering
    the legacy ``diff_lines`` count.
    """
    if not diff_out:
        return diff_out
    out_parts = []
    skip_current = False
    for line in diff_out.splitlines(keepends=True):
        if line.startswith("diff --git "):
            # Parse `diff --git a/<path> b/<path>`. The b-path is everything
            # after the last ` b/` occurrence on the line; quoted paths with
            # spaces still terminate at end-of-line.
            b_marker = " b/"
            idx = line.rfind(b_marker)
            if idx != -1:
                b_path = line[idx + len(b_marker):].rstrip("\n").strip().strip('"')
                basename = os.path.basename(b_path)
                skip_current = basename in GENERATED_LOCKFILES
            else:
                skip_current = False
        if not skip_current:
            out_parts.append(line)
    return "".join(out_parts)
