#!/usr/bin/env python3
"""Create and inspect task-owned git worktrees."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-json-lib.py, and tusk-git-helpers.py

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection

_json = tusk_loader.load("tusk-json-lib")
dumps = _json.dumps

_git_helpers = tusk_loader.load("tusk-git-helpers")
task_referenced_paths = _git_helpers.task_referenced_paths

# Canonical runtime artifacts auto-linked when `worktree.symlink_files` is
# empty (issue #854). install.sh-only installs never run the project_type
# auto-seed in `init-write-config`, leaving the list empty even for projects
# that obviously need these files. The fallback links them anyway and prints
# a stderr advisory pointing at /tusk-update so the implicit list can be made
# explicit; `TUSK_NO_AUTO_SYMLINK=1` disables it.
CANONICAL_RUNTIME_FILES = ["node_modules", ".venv", ".env", ".env.local"]

# Marker file written into a namespace subdir on first claim — names the
# absolute primary-repo path that owns the subdir. Future create calls read
# it to disambiguate same-basename collisions across repos (TASK-468).
PRIMARY_MARKER_FILE = ".tusk-primary"


def _namespace_for(workspace_root: str, repo_root: str, *, claim: bool = True) -> str:
    """Return the per-repo namespace subdir name under ``workspace_root``.

    Default is ``os.path.basename(<primary_repo_root>)``. If that subdir
    already exists with a ``.tusk-primary`` marker naming a different repo,
    fall back to ``<basename>-<sha256(repo_root)[:6]>`` so two repos with
    the same basename never share the worktree pool. First creation writes
    the marker so future creates resolve in O(1) (one read + compare).

    Hash input is the absolute primary-repo path — NOT the git remote URL,
    which is unreliable (missing/multiple remotes, fork-vs-upstream).

    Best-effort: marker write failures are swallowed so a read-only home
    directory cannot break worktree creation. The fallback hash form never
    depends on a marker, so a swallowed write still yields a usable path
    (the same call site will recompute the same namespace on every retry).

    Pass ``claim=False`` to compute the namespace without creating the
    namespace dir or writing the marker file — used by ``relocate
    --dry-run`` so the planning phase never touches the filesystem.
    """
    primary = _primary_repo_root(os.path.abspath(repo_root))
    basename = os.path.basename(primary.rstrip(os.sep)) or "tusk"
    candidate_dir = os.path.join(workspace_root, basename)
    marker_path = os.path.join(candidate_dir, PRIMARY_MARKER_FILE)

    def _claim(dst: str) -> None:
        if not claim:
            return
        try:
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, PRIMARY_MARKER_FILE), "w", encoding="utf-8") as fh:
                fh.write(primary + "\n")
        except OSError:
            pass

    if not os.path.isdir(candidate_dir):
        _claim(candidate_dir)
        return basename

    existing_marker: str | None = None
    if os.path.isfile(marker_path):
        try:
            with open(marker_path, encoding="utf-8") as fh:
                existing_marker = fh.read().strip() or None
        except OSError:
            existing_marker = None

    if existing_marker is None:
        # Dir exists but unclaimed — claim it for this repo. The dir may have
        # been created by a prior tusk version, by `mkdir -p`, or by an
        # earlier _namespace_for call that failed mid-write; in all cases
        # taking ownership is safe because no marker means no other repo
        # has staked a claim.
        _claim(candidate_dir)
        return basename

    if existing_marker == primary:
        return basename

    # Collision: marker names a different repo. Hash form is keyed to this
    # repo's path, so a parallel call from another colliding repo would get
    # its own distinct hash subdir.
    digest = hashlib.sha256(primary.encode("utf-8")).hexdigest()[:6]
    return f"{basename}-{digest}"


def _list_workspaces(conn: sqlite3.Connection) -> list[dict]:
    return _list_workspaces_with_live_state(conn, {})


def _is_stale_workspace(row: dict) -> bool:
    return not row["exists_on_disk"] and row["live_workspace_path"] is None


def _resolve_task_id(raw: str) -> int:
    value = raw.strip()
    if value.upper().startswith("TASK-"):
        value = value[5:]
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid task ID: {raw}") from exc


def _run_git(repo_root: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _detect_default_branch(repo_root: str) -> str:
    set_head = _run_git(repo_root, ["remote", "set-head", "origin", "--auto"])
    if set_head.returncode == 0:
        origin_head = _run_git(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"])
        if origin_head.returncode == 0 and origin_head.stdout.strip():
            return origin_head.stdout.strip().replace("refs/remotes/origin/", "")

    for candidate in ("main", "master"):
        exists = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{candidate}"])
        if exists.returncode == 0:
            return candidate

    current = _run_git(repo_root, ["branch", "--show-current"])
    if current.returncode == 0 and current.stdout.strip():
        return current.stdout.strip()
    return "main"


def _branch_exists(repo_root: str, branch: str) -> bool:
    result = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"])
    return result.returncode == 0


def _origin_remote_exists(repo_root: str) -> bool:
    result = _run_git(repo_root, ["remote", "get-url", "origin"])
    return result.returncode == 0


def _remote_branch_exists(repo_root: str, branch: str) -> bool:
    result = _run_git(
        repo_root,
        ["show-ref", "--verify", f"refs/remotes/origin/{branch}"],
    )
    return result.returncode == 0


def _resolve_worktree_base(repo_root: str) -> tuple[bool, str, str]:
    default_branch = _detect_default_branch(repo_root)
    if not _origin_remote_exists(repo_root):
        return True, default_branch, ""

    fetch = _run_git(repo_root, ["fetch", "origin"])
    if fetch.returncode != 0:
        return (
            False,
            "",
            "could not refresh origin before creating task workspace:\n"
            f"{fetch.stderr.strip()}",
        )

    default_branch = _detect_default_branch(repo_root)
    if _remote_branch_exists(repo_root, default_branch):
        return True, f"origin/{default_branch}", ""
    return True, default_branch, ""


def _create_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str,
    base_branch: str,
) -> tuple[bool, str]:
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    result = _run_git(
        repo_root,
        ["worktree", "add", "-b", branch, worktree_path, base_branch],
    )
    return result.returncode == 0, result.stderr.strip()


def _load_scope_list(config_path: str, key: str) -> list[str]:
    """Load ``scope.<key>`` from the project config, returning [] on any error."""
    if not config_path or not os.path.exists(config_path):
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    scope_cfg = cfg.get("scope")
    if not isinstance(scope_cfg, dict):
        return []
    values = scope_cfg.get(key)
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if isinstance(v, str) and v]


def _test_command_cone_paths(config_path: str) -> list[str]:
    """Extract path-shaped tokens from the configured global ``test_command``.

    Returns the list of tokens that look like file or directory paths
    (contain ``/``, don't start with ``-``, don't contain ``=``), so they
    can be unioned into the sparse-checkout cone at worktree-create time.
    This is the issue #892 fix: a task whose referenced paths exclude
    ``tests/unit/`` but whose configured ``test_command`` is
    ``python3 -m pytest tests/unit/ -q`` would otherwise fail every
    ``tusk commit`` test gate with "file or directory not found" until
    the operator manually extended the cone.

    Heuristic-only — does not parse shell syntax. ``path_test_commands``
    and ``domain_test_commands`` overrides are NOT included here because
    they depend on staged paths and task domain that aren't known at
    create time; the global ``test_command`` is the conservative
    fallback that always runs.
    """
    if not config_path or not os.path.exists(config_path):
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    cmd = cfg.get("test_command") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    paths: list[str] = []
    for tok in cmd.split():
        tok = tok.strip().strip('"').strip("'")
        if not tok:
            continue
        if tok.startswith("-") or "=" in tok:
            continue
        if "/" in tok:
            paths.append(tok)
    return paths


def _is_safe_cone_entry(entry: str) -> bool:
    """Return True iff ``entry`` is safe to pass to ``git sparse-checkout set``.

    Rejects entries that ``git sparse-checkout`` will refuse (and that produce
    the ``fatal: could not normalize path ..`` failure observed in issue #928):

    - Absolute paths (cone is repo-root-relative; an absolute entry gets
      stripped of its leading ``/`` or rejected outright).
    - Any segment equal to ``..`` (parent traversal) — this is the literal
      ``could not normalize path ..`` trigger.
    - Empty-string segments (e.g. ``foo//bar``) which normalize to ``foo/bar``
      but signal a malformed input upstream.

    Single-segment ``.`` entries are normalized to empty by ``os.path.normpath``
    and rejected here as a no-op (cone mode auto-includes top-level files).
    """
    if not entry:
        return False
    if entry.startswith("/"):
        return False
    parts = entry.split("/")
    for seg in parts:
        if seg == "..":
            return False
        if seg == "" and entry != "/":
            return False
    return True


def _normalize_cone_entry(entry: str) -> str:
    """Return a normalized cone entry, or ``""`` if it must be dropped.

    Strips whitespace, leading ``./``, trailing ``/``, then runs
    ``os.path.normpath`` to collapse interior ``./`` segments. The result is
    only returned when ``_is_safe_cone_entry`` passes; otherwise the empty
    string signals "drop this entry".
    """
    if not entry:
        return ""
    s = entry.strip().rstrip("/")
    while s.startswith("./"):
        s = s[2:]
    if not s or s == ".":
        return ""
    normalized = os.path.normpath(s)
    if normalized in {".", ""}:
        return ""
    if not _is_safe_cone_entry(normalized):
        return ""
    return normalized


def _derive_sparse_cone(paths: list[str]) -> list[str]:
    """Derive cone-mode sparse-checkout directory entries from a path list.

    Root-level entries (no ``/``) are dropped — cone mode auto-includes every
    file at the toplevel of the worktree, and ``git sparse-checkout set`` in
    cone mode rejects file paths anyway. Nested entries contribute their
    parent directory (e.g. ``.claude/tusk-manifest.json`` → ``.claude``,
    ``tests/integration/test_a.py`` → ``tests/integration``). Returns a
    sorted unique list, with entries that ``git sparse-checkout`` would
    reject (absolute paths, ``..`` segments) filtered out — they were the
    trigger for the ``could not normalize path ..`` failure in issue #928.
    """
    cone: set[str] = set()
    for p in paths:
        if not p:
            continue
        p = p.strip().rstrip("/")
        if not p or "/" not in p:
            continue
        candidate = _normalize_cone_entry(os.path.dirname(p))
        if candidate:
            cone.add(candidate)
    return sorted(cone)


def _apply_sparse_checkout(
    worktree_path: str, cone: list[str]
) -> tuple[bool, bool, str]:
    """Initialize cone-mode sparse-checkout on ``worktree_path`` and set the cone.

    Runs ``git sparse-checkout init --cone`` (which auto-enables
    ``extensions.worktreeConfig`` so the resulting state is per-worktree and
    does not affect the primary checkout) followed by
    ``git sparse-checkout set <cone>`` when ``cone`` is non-empty.

    Returns ``(applied, disabled_fallback, stderr)``:

    - ``applied=True, disabled_fallback=False`` — sparse-checkout is active
      and the cone is set as requested.
    - ``applied=False, disabled_fallback=True`` — init or set failed AND
      ``git sparse-checkout disable`` succeeded as the fallback, so the
      working tree is fully materialized (matching the "falls back to a
      full checkout" advisory the caller prints). ``stderr`` carries the
      original sparse-checkout failure reason.
    - ``applied=False, disabled_fallback=False`` — both sparse-checkout
      setup AND the disable fallback failed; the worktree is in an
      indeterminate state and the caller must surface a clear error.
      ``stderr`` carries both failure reasons joined by ``" || disable: "``.

    Sparse-checkout is an optimization; the function never blocks worktree
    creation, but it must also never leave the worktree in a partial-sparse
    state that the caller has advertised as a full checkout (issue #928).
    """
    init = _run_git(worktree_path, ["sparse-checkout", "init", "--cone"])
    if init.returncode != 0:
        return _disable_fallback(worktree_path, init.stderr.strip())
    if not cone:
        return True, False, ""
    set_result = _run_git(
        worktree_path, ["sparse-checkout", "set", *cone]
    )
    if set_result.returncode != 0:
        return _disable_fallback(worktree_path, set_result.stderr.strip())
    return True, False, ""


def _disable_fallback(
    worktree_path: str, sparse_err: str
) -> tuple[bool, bool, str]:
    """Run ``git sparse-checkout disable`` to materialize a real full checkout.

    Called from ``_apply_sparse_checkout`` after init or set fails — the
    sparse-checkout state at this point is "enabled but empty / partial",
    which leaves the worktree at ~1% of tracked files (issue #928). The
    disable call un-sets ``core.sparseCheckout`` and re-materializes the
    full tree. Returns the tri-state ``(applied, disabled_fallback, stderr)``
    contract documented on ``_apply_sparse_checkout``.
    """
    disable = _run_git(worktree_path, ["sparse-checkout", "disable"])
    if disable.returncode == 0:
        return False, True, sparse_err
    combined = f"{sparse_err} || disable: {disable.stderr.strip()}"
    return False, False, combined


def _primary_repo_root(repo_root: str) -> str:
    """Resolve the primary checkout's root from a possibly-worktree ``repo_root``.

    ``repo_root`` is whatever the dispatcher passed in (cwd-resolved). In a
    linked worktree, ``git --git-common-dir`` points at the primary's ``.git``;
    the parent of that is the primary checkout. In the primary itself, the
    common-dir is the same as the git-dir and the parent IS the primary root.
    Falls back to ``repo_root`` on any git error so symlink seeding is best-
    effort and never breaks worktree creation.
    """
    result = _run_git(
        repo_root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if result.returncode != 0:
        return repo_root
    common_dir = result.stdout.strip()
    if not common_dir:
        return repo_root
    primary = os.path.dirname(common_dir)
    return primary if os.path.isdir(primary) else repo_root


def _load_symlink_files(config_path: str) -> list[str]:
    """Load ``worktree.symlink_files`` from the project config, returning [] on any error."""
    if not config_path or not os.path.exists(config_path):
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    worktree_cfg = cfg.get("worktree")
    if not isinstance(worktree_cfg, dict):
        return []
    names = worktree_cfg.get("symlink_files")
    if not isinstance(names, list):
        return []
    # Filter to non-empty strings; ignore None / empty / non-string entries.
    return [str(n) for n in names if isinstance(n, str) and n]


def _link_gitignored_files(
    primary_root: str,
    worktree_path: str,
    names: list[str],
) -> list[dict]:
    """Symlink configured entries from ``primary_root`` into ``worktree_path``.

    Entries are partitioned by shape:

    - **Bare basenames** (no ``/``) — walk ``primary_root`` for files/dirs
      whose basename appears in the configured set. Every match is symlinked
      at the corresponding relative path under ``worktree_path``. Skips
      ``.git``; never follows symlinks during the walk. This is the original
      behavior (issue #752).
    - **Path-style entries** (contain ``/``) — treated as project-relative
      paths. Exactly one symlink is created at ``worktree_path/<entry>``
      pointing back to ``primary_root/<entry>`` iff the primary target exists.
      No walking, no over-matching nested copies — gives monorepo users a way
      to scope (e.g. ``apps/web/node_modules``) without linking every nested
      ``node_modules`` (issue #867).

    Path-style entries are validated: a leading ``/``, an empty segment (``//``
    or trailing ``/``), or any ``.`` / ``..`` segment is rejected silently —
    these could escape the primary checkout or yield ambiguous targets.

    Skips entries whose worktree destination already exists.

    Returns ``[{"src": <primary_path>, "dst": <worktree_path>}, ...]`` for
    each symlink that was actually created.
    """
    if not names:
        return []

    basenames: list[str] = []
    path_entries: list[str] = []
    for name in names:
        if "/" not in name:
            basenames.append(name)
            continue
        if name.startswith("/"):
            continue
        parts = name.split("/")
        if any(p in ("", ".", "..") for p in parts):
            continue
        path_entries.append(name)

    created: list[dict] = []

    def _try_link(src: str, dst: str) -> None:
        # `lexists` catches files, dirs, and symlinks (including broken).
        if os.path.lexists(dst):
            return
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(src, dst)
        except OSError:
            # Best-effort: do not abort worktree creation on a single
            # failed symlink (permission errors, race conditions, etc.).
            return
        created.append({"src": src, "dst": dst})

    # Path-style entries first so a later bare-basename walk that would match
    # the same leaf (e.g. ".venv" basename + "apps/scraper/.venv" path-style)
    # sees the destination already present and skips it.
    for rel in path_entries:
        src = os.path.join(primary_root, rel)
        if not os.path.lexists(src):
            continue
        dst = os.path.join(worktree_path, rel)
        _try_link(src, dst)

    if basenames:
        name_set = set(basenames)
        for root, dirs, files in os.walk(primary_root, followlinks=False):
            if ".git" in dirs:
                dirs.remove(".git")
            # Capture matched dir names BEFORE we mutate `dirs` to control recursion.
            matched_dirs = [d for d in dirs if d in name_set]
            matched_files = [f for f in files if f in name_set]
            for name in matched_dirs + matched_files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, primary_root)
                dst = os.path.join(worktree_path, rel)
                _try_link(src, dst)
            # Prevent os.walk from descending INTO any directory we just symlinked
            # — the symlink target already contains its full subtree.
            for d in matched_dirs:
                dirs.remove(d)

    return created


def _attach_worktree(
    repo_root: str,
    worktree_path: str,
    branch: str,
) -> tuple[bool, str]:
    """Re-attach a worktree at ``worktree_path`` checked out on existing ``branch``.

    Mirrors ``_create_worktree`` but omits ``-b`` so an existing branch is
    reused rather than recreated (issue #803). Used when a ``task_workspaces``
    row exists, its branch still resolves in git, but ``workspace_path`` was
    deleted from disk — the canonical recovery path that avoids forcing the
    caller to prune-and-retry.
    """
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    result = _run_git(
        repo_root,
        ["worktree", "add", worktree_path, branch],
    )
    return result.returncode == 0, result.stderr.strip()


def _parse_git_worktrees(repo_root: str) -> dict[str, str]:
    result = _run_git(repo_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return {}

    by_branch: dict[str, str] = {}
    current_path = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/") and current_path:
            branch = line[len("branch refs/heads/"):].strip()
            by_branch[branch] = current_path
    return by_branch


def _auto_prune_stale_workspaces(
    conn: sqlite3.Connection, repo_root: str, exclude_task_id: int
) -> int:
    """Drop registry rows whose ``workspace_path`` is gone AND not in ``git worktree list``.

    Same staleness predicate as ``tusk task-worktree prune`` (``_is_stale_workspace``),
    scoped to exclude ``exclude_task_id`` so the per-task reconcile logic in
    ``cmd_create`` (re-attach when branch survives, refuse when fully stale) runs
    intact for the current task's own row. Returns the count of rows deleted.
    """
    stale = [
        row
        for row in _list_workspaces_with_live_state(
            conn, _parse_git_worktrees(repo_root)
        )
        if _is_stale_workspace(row) and row["task_id"] != exclude_task_id
    ]
    if stale:
        conn.executemany(
            "DELETE FROM task_workspaces WHERE id = ?",
            [(row["workspace_id"],) for row in stale],
        )
        conn.commit()
    return len(stale)


def _list_workspaces_with_live_state(
    conn: sqlite3.Connection, live_by_branch: dict[str, str]
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, task_id, branch, workspace_path, created_at, updated_at
        FROM task_workspaces
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "workspace_id": row["id"],
            "task_id": row["task_id"],
            "branch": row["branch"],
            "workspace_path": row["workspace_path"],
            "exists_on_disk": os.path.isdir(row["workspace_path"]),
            "live_workspace_path": live_by_branch.get(row["branch"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _fetch_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, bakeoff_shadow FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _workspace_payload(row: sqlite3.Row, *, created: bool) -> dict:
    return {
        "workspace_id": row["id"],
        "task_id": row["task_id"],
        "branch": row["branch"],
        "workspace_path": row["workspace_path"],
        "created": created,
    }


def cmd_create(
    db_path: str, config_path: str, repo_root: str, argv: list[str]
) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree create",
        description="Create or reuse a task-owned git worktree.",
    )
    parser.add_argument("task_id", help="Task ID as an integer or TASK-NNN.")
    parser.add_argument("slug", help="Branch slug for feature/TASK-<id>-<slug>.")
    parser.add_argument(
        "--workspace-root",
        default=None,
        help=(
            "Parent directory for task worktrees. Default: $TUSK_WORKTREE_ROOT "
            "or $HOME/.tusk/worktrees."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Override path to tusk/config.json for this invocation — use to "
            "verify dispatcher-consumed config changes (e.g. "
            "worktree.symlink_files) from a feature worktree before merging. "
            "Default: primary checkout's tusk/config.json via dispatcher."
        ),
    )
    parser.add_argument(
        "--cone",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Pre-declare extra sparse-checkout cone paths (repeatable). "
            "Unioned with task_referenced_paths, scope.sparse_always_include, "
            "scope.always_allowed, and the configured test_command's target "
            "paths. Skipped entirely when sparse-checkout itself is disabled "
            "(zero referenced paths or TUSK_NO_SPARSE_WORKTREE=1)."
        ),
    )
    args = parser.parse_args(argv)

    if args.config is not None:
        if not os.path.isfile(args.config):
            print(
                f"Error: --config path does not exist or is not a regular file: "
                f"{args.config}",
                file=sys.stderr,
            )
            return 1
        config_path = args.config

    try:
        task_id = _resolve_task_id(args.task_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    slug = args.slug.strip().strip("/")
    if not slug:
        print("Error: Slug must not be empty", file=sys.stderr)
        return 1

    branch = f"feature/TASK-{task_id}-{slug}"
    workspace_root = (
        args.workspace_root
        or os.environ.get("TUSK_WORKTREE_ROOT")
        or os.path.join(os.path.expanduser("~"), ".tusk", "worktrees")
    )
    # workspace_path is computed lazily on the new-create branch below so
    # existing rows keep their persisted path (which may be from the legacy
    # flat-pool layout) and so the marker write only fires when we are
    # actually about to create a fresh worktree (TASK-468).

    conn = get_connection(db_path)
    try:
        task = _fetch_task(conn, task_id)
        if task is None:
            print(f"Error: task {task_id} not found", file=sys.stderr)
            return 1
        if task["bakeoff_shadow"]:
            print(
                f"Error: TASK-{task_id} is a bakeoff shadow; task worktrees "
                "must target normal tasks.",
                file=sys.stderr,
            )
            return 1

        # Reconcile sibling tasks' stale registry rows before adding a new
        # workspace, so registry accumulation is capped without operator
        # effort (TASK-477). Scoped to ``task_id != exclude_task_id`` so the
        # issue #803 reconcile logic (re-attach when branch survives, refuse
        # when fully stale) for THIS task's own row runs intact.
        if not os.environ.get("TUSK_NO_AUTO_PRUNE"):
            _auto_prune_stale_workspaces(conn, repo_root, task_id)

        existing = conn.execute(
            """
            SELECT id, task_id, branch, workspace_path
            FROM task_workspaces
            WHERE task_id = ? AND branch = ?
            """,
            (task_id, branch),
        ).fetchone()
        if existing:
            # Healthy state: registry row + workspace_path present on disk.
            if os.path.isdir(existing["workspace_path"]):
                print(dumps(_workspace_payload(existing, created=False)))
                return 0
            # Stale state (issue #803): registry row exists but workspace_path
            # is gone from disk. The caller would otherwise `cd` into a
            # dangling path. Recover when the branch still resolves in git;
            # refuse loudly when it does not.
            if _branch_exists(repo_root, existing["branch"]):
                ok, err = _attach_worktree(
                    repo_root,
                    existing["workspace_path"],
                    existing["branch"],
                )
                if not ok:
                    print(
                        "Error: recorded workspace path is missing on disk and "
                        f"`git worktree add` could not re-attach it:\n"
                        f"  Workspace path: {existing['workspace_path']}\n"
                        f"  Branch:         {existing['branch']}\n"
                        f"  git stderr:     {err}\n"
                        f"  Hint: run `tusk task-worktree prune` to drop the stale row, "
                        f"then re-run `tusk task-worktree create {task_id} {slug}` "
                        f"to materialize a fresh workspace.",
                        file=sys.stderr,
                    )
                    return 2
                print(dumps(_workspace_payload(existing, created=True)))
                return 0
            # Both row and disk and branch are gone — registry is fully stale.
            print(
                "Error: recorded workspace is unusable — both the workspace "
                "path and the branch are missing:\n"
                f"  Workspace path: {existing['workspace_path']}\n"
                f"  Branch:         {existing['branch']}\n"
                f"  Hint: run `tusk task-worktree prune` to drop the stale row, "
                f"then re-run `tusk task-worktree create {task_id} {slug}` "
                f"to materialize a fresh workspace.",
                file=sys.stderr,
            )
            return 2

        if _branch_exists(repo_root, branch):
            print(
                f"Error: branch '{branch}' already exists but is not recorded "
                "as a task workspace.",
                file=sys.stderr,
            )
            return 2

        base_ok, base_branch, base_err = _resolve_worktree_base(repo_root)
        if not base_ok:
            print(f"Error: {base_err}", file=sys.stderr)
            return 2

        # Resolve the per-repo namespace just before the actual create so the
        # marker write never fires for reused existing rows above (TASK-468).
        namespace = _namespace_for(workspace_root, repo_root)
        workspace_path = os.path.join(
            workspace_root, namespace, f"TASK-{task_id}-{slug}"
        )

        ok, err = _create_worktree(
            repo_root,
            workspace_path,
            branch,
            base_branch,
        )
        if not ok:
            print(f"Error: git worktree add failed:\n{err}", file=sys.stderr)
            return 2

        # Apply cone-mode sparse-checkout when the task has referenced paths,
        # so the worktree materializes only the task scope plus the always-
        # include and always-allowed sets (TASK-470). Skipped when the task
        # has no referenced paths (full checkout — the pre-TASK-470 default)
        # or when TUSK_NO_SPARSE_WORKTREE=1. Best-effort: a sparse-checkout
        # failure prints an advisory and continues, never blocking create.
        #
        # Cone sources unioned together (TASK-480, issues #892/#896):
        #   1. task_referenced_paths — extracted from the task description
        #      and criteria.
        #   2. scope.sparse_always_include — project-level "always materialize"
        #      paths from tusk/config.json (file paths; dirname extracted).
        #   3. scope.always_allowed — auto-allowed files (VERSION, MANIFEST,
        #      etc.); cone derivation drops root-level entries.
        #   4. test_command's target paths — so `tusk commit`'s default test
        #      gate (typically `python3 -m pytest tests/unit/`) does not fail
        #      with "file or directory not found" the first time it runs
        #      (issue #892, criterion 2230).
        #   5. --cone <path> CLI flag — operator-declared extras for tasks
        #      that obviously touch skills/docs/hooks without describing
        #      every path up front (issue #896, criterion 2231).
        #   6. scope.sparse_always_cone — project-level "always materialize"
        #      cone directories from tusk/config.json (literal directory
        #      entries; no dirname extraction). Right for source-repo
        #      configs that want to force `.claude/`, `skills/`, `.github/`,
        #      etc. into every task worktree so unit tests reading those
        #      files don't FileNotFoundError under a narrow per-task cone
        #      (issue #935).
        if not os.environ.get("TUSK_NO_SPARSE_WORKTREE"):
            referenced = task_referenced_paths(task_id, conn)
            if referenced:
                always_include = _load_scope_list(
                    config_path, "sparse_always_include"
                )
                always_allowed = _load_scope_list(config_path, "always_allowed")
                always_cone = _load_scope_list(config_path, "sparse_always_cone")
                test_cmd_paths = _test_command_cone_paths(config_path)
                # File-path inputs go through _derive_sparse_cone, which drops
                # root-level entries (cone mode auto-materializes top-level
                # files) and takes the parent dir of nested file paths.
                cone_set = set(
                    _derive_sparse_cone(
                        [
                            *referenced,
                            *always_include,
                            *always_allowed,
                            *test_cmd_paths,
                        ]
                    )
                )
                # sparse_always_cone entries are directory-shaped; pass them
                # through _normalize_cone_entry without the dirname() step
                # so `skills` lands as `skills` rather than being dropped as
                # a single-segment entry (issue #935).
                for raw in always_cone:
                    d = _normalize_cone_entry(raw or "")
                    if d:
                        cone_set.add(d)
                # --cone <path> entries are directory-shaped; pass them through
                # without the dirname() step so `--cone docs` survives the
                # single-segment drop and `--cone skills/tusk` lands as a
                # targeted subtree entry rather than being widened to `skills`
                # (issue #896). They still go through _normalize_cone_entry so
                # absolute paths and `..` segments get filtered out before
                # reaching `git sparse-checkout set` (issue #928).
                for raw in args.cone:
                    d = _normalize_cone_entry(raw or "")
                    if d:
                        cone_set.add(d)
                cone = sorted(cone_set)
                sparse_applied, sparse_disabled, sparse_err = (
                    _apply_sparse_checkout(workspace_path, cone)
                )
                if sparse_applied:
                    cone_display = ", ".join(cone) if cone else "(root only)"
                    print(
                        f"Note: sparse-checkout applied (cone: {cone_display}). "
                        "Extend in-worktree via "
                        "`git sparse-checkout add <path>`.",
                        file=sys.stderr,
                    )
                elif sparse_disabled:
                    print(
                        "Note: sparse-checkout setup failed; worktree falls "
                        f"back to a full checkout. git stderr: {sparse_err}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "Warning: sparse-checkout setup failed AND the "
                        "full-checkout fallback (git sparse-checkout disable) "
                        "also failed; the worktree is in a partial-sparse "
                        "state with an empty or unset cone. Run "
                        "`git -C "
                        f"{workspace_path} sparse-checkout disable` manually "
                        f"to recover. git stderr: {sparse_err}",
                        file=sys.stderr,
                    )

        cur = conn.execute(
            """
            INSERT INTO task_workspaces (task_id, branch, workspace_path)
            VALUES (?, ?, ?)
            """,
            (task_id, branch, workspace_path),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, task_id, branch, workspace_path
            FROM task_workspaces
            WHERE id = ?
            """,
            (cur.lastrowid,),
        ).fetchone()
        # Seed gitignored runtime files (e.g. .venv, .env) from the primary
        # repo per worktree.symlink_files config (issue #752), or — when that
        # list is empty and TUSK_NO_AUTO_SYMLINK is unset — fall back to the
        # canonical name set so install.sh-only installs that never ran the
        # init-write-config auto-seed still pick up node_modules / .venv /
        # .env / .env.local (issue #854). Best-effort throughout: individual
        # symlink failures are swallowed inside _link_gitignored_files.
        symlink_names = _load_symlink_files(config_path)
        is_fallback = False
        if not symlink_names and not os.environ.get("TUSK_NO_AUTO_SYMLINK"):
            symlink_names = list(CANONICAL_RUNTIME_FILES)
            is_fallback = True
        if symlink_names:
            primary_root = _primary_repo_root(repo_root)
            created = _link_gitignored_files(
                primary_root, workspace_path, symlink_names
            )
            if is_fallback and created:
                linked_basenames = sorted({os.path.basename(c["dst"]) for c in created})
                print(
                    "Note: auto-linked "
                    + ", ".join(linked_basenames)
                    + " from primary (worktree.symlink_files is empty). "
                    "Run /tusk-update to set the list explicitly, or "
                    "TUSK_NO_AUTO_SYMLINK=1 to disable this fallback.",
                    file=sys.stderr,
                )
        print(dumps(_workspace_payload(row, created=True)))
        return 0
    except sqlite3.IntegrityError as exc:
        print(f"Error: could not record task workspace: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def cmd_list(db_path: str, repo_root: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree list",
        description="List recorded task-owned git worktrees.",
    )
    parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format (default: json).",
    )
    parser.parse_args(argv)

    conn = get_connection(db_path)
    try:
        print(dumps(_list_workspaces_with_live_state(conn, _parse_git_worktrees(repo_root))))
    finally:
        conn.close()
    return 0


def _worktree_is_clean(workspace_path: str) -> tuple[bool, str]:
    """Return ``(clean, raw_status)`` for ``workspace_path``.

    ``clean`` is True when ``git status --porcelain`` produces no output. The
    second element is the raw porcelain text so callers can surface what was
    dirty in error messages. Missing directories report as not clean with a
    synthetic reason — reconcile should refuse to touch them rather than fall
    through and let a downstream git command produce a confusing error.
    """
    if not os.path.isdir(workspace_path):
        return False, "(workspace path missing)"
    result = subprocess.run(
        ["git", "-C", workspace_path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return False, (result.stderr.strip() or "(git status failed)")
    text = result.stdout
    return (text.strip() == ""), text


def _branch_is_merged(repo_root: str, branch: str, default_branch: str) -> bool:
    """Return True when every commit on ``branch`` is an ancestor of ``default_branch``."""
    result = _run_git(
        repo_root,
        ["merge-base", "--is-ancestor", branch, default_branch],
    )
    return result.returncode == 0


def _classify_reconcile_row(
    conn: sqlite3.Connection,
    repo_root: str,
    row: dict,
    default_branch: str,
) -> dict:
    """Augment ``row`` with task status / merged / clean / eligibility fields.

    Eligibility = task Done AND branch fully merged into default AND worktree
    clean AND branch still resolvable. Any miss is recorded under ``reason``
    so JSON consumers (and the per-row prompt) can explain the skip.
    """
    task = conn.execute(
        "SELECT status, closed_reason FROM tasks WHERE id = ?",
        (row["task_id"],),
    ).fetchone()
    task_status = task["status"] if task else None
    closed_reason = task["closed_reason"] if task else None

    branch_present = _branch_exists(repo_root, row["branch"])
    if branch_present:
        merged = _branch_is_merged(repo_root, row["branch"], default_branch)
    else:
        merged = False
    clean, dirty_detail = _worktree_is_clean(row["workspace_path"])

    eligible = (
        task_status == "Done"
        and branch_present
        and merged
        and clean
    )
    reasons: list[str] = []
    if task_status != "Done":
        reasons.append(f"task not Done (status={task_status!r})")
    if not branch_present:
        reasons.append(f"branch {row['branch']!r} not found in local refs")
    elif not merged:
        reasons.append(
            f"branch {row['branch']!r} not fully merged into {default_branch!r}"
        )
    if not clean:
        reasons.append(f"worktree not clean: {dirty_detail.strip() or '(unknown)'}")

    return {
        **row,
        "task_status": task_status,
        "closed_reason": closed_reason,
        "branch_present": branch_present,
        "merged_into_default": merged,
        "clean": clean,
        "eligible": eligible,
        "skip_reasons": reasons,
    }


def _perform_reconcile(
    conn: sqlite3.Connection,
    repo_root: str,
    row: dict,
) -> tuple[bool, list[str]]:
    """Remove the worktree, delete the branch, drop the registry row.

    Returns ``(ok, errors)``. Each step is best-effort independent of the
    others — registry row is always dropped last so a partial git failure
    still surfaces and the operator can clean up by hand without losing
    DB consistency.
    """
    errors: list[str] = []

    if os.path.isdir(row["workspace_path"]):
        result = _run_git(
            repo_root,
            ["worktree", "remove", row["workspace_path"]],
        )
        if result.returncode != 0:
            errors.append(
                f"git worktree remove {row['workspace_path']} failed: "
                f"{(result.stderr.strip() or result.stdout.strip())}"
            )
            return False, errors

    if _branch_exists(repo_root, row["branch"]):
        result = _run_git(repo_root, ["branch", "-D", row["branch"]])
        if result.returncode != 0:
            errors.append(
                f"git branch -D {row['branch']} failed: "
                f"{(result.stderr.strip() or result.stdout.strip())}"
            )

    conn.execute("DELETE FROM task_workspaces WHERE id = ?", (row["workspace_id"],))
    conn.commit()
    return True, errors


def cmd_reconcile(db_path: str, repo_root: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree reconcile",
        description=(
            "Clean up worktrees whose tasks are Done and whose branches are "
            "already fully merged into the default branch. Refuses to touch "
            "dirty worktrees or unmerged branches."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without removing anything.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip per-worktree confirmation prompts.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args(argv)

    conn = get_connection(db_path)
    try:
        default_branch = _detect_default_branch(repo_root)
        live_by_branch = _parse_git_worktrees(repo_root)
        rows = _list_workspaces_with_live_state(conn, live_by_branch)
        classified = [
            _classify_reconcile_row(conn, repo_root, row, default_branch)
            for row in rows
        ]
        eligible = [r for r in classified if r["eligible"]]
        skipped = [r for r in classified if not r["eligible"]]

        if args.format == "json":
            results: list[dict] = []
            removed_count = 0
            if not args.dry_run:
                for row in eligible:
                    ok, errors = _perform_reconcile(conn, repo_root, row)
                    if ok:
                        removed_count += 1
                    results.append({
                        "task_id": row["task_id"],
                        "branch": row["branch"],
                        "workspace_path": row["workspace_path"],
                        "ok": ok,
                        "errors": errors,
                    })
            print(
                dumps(
                    {
                        "dry_run": args.dry_run,
                        "default_branch": default_branch,
                        "eligible": eligible,
                        "skipped": skipped,
                        "removed_count": removed_count,
                        "results": results,
                    }
                )
            )
            return 0

        # Text mode.
        if not eligible:
            print(
                f"No eligible worktrees to reconcile (default branch: "
                f"{default_branch}).",
                file=sys.stderr,
            )
            for row in skipped:
                print(
                    f"  skip TASK-{row['task_id']} branch={row['branch']}: "
                    + "; ".join(row["skip_reasons"]),
                    file=sys.stderr,
                )
            return 0

        print(
            f"Reconcile plan ({len(eligible)} eligible, default branch: "
            f"{default_branch}):",
            file=sys.stderr,
        )
        for row in eligible:
            print(
                f"  TASK-{row['task_id']} branch={row['branch']} "
                f"path={row['workspace_path']}",
                file=sys.stderr,
            )
        if skipped:
            print(f"Skipped ({len(skipped)}):", file=sys.stderr)
            for row in skipped:
                print(
                    f"  TASK-{row['task_id']} branch={row['branch']}: "
                    + "; ".join(row["skip_reasons"]),
                    file=sys.stderr,
                )

        if args.dry_run:
            print("Dry run — no changes made.", file=sys.stderr)
            return 0

        removed = 0
        for row in eligible:
            if not args.yes:
                prompt = (
                    f"Remove worktree for TASK-{row['task_id']} "
                    f"({row['workspace_path']})? [y/N] "
                )
                try:
                    answer = input(prompt).strip().lower()
                except EOFError:
                    answer = ""
                if answer not in {"y", "yes"}:
                    print(
                        f"  skipped TASK-{row['task_id']} (declined)",
                        file=sys.stderr,
                    )
                    continue
            ok, errors = _perform_reconcile(conn, repo_root, row)
            if ok:
                removed += 1
                print(
                    f"  removed TASK-{row['task_id']} ({row['workspace_path']})",
                    file=sys.stderr,
                )
            else:
                for err in errors:
                    print(f"  error TASK-{row['task_id']}: {err}", file=sys.stderr)
            for err in errors:
                if ok:
                    print(f"  warning TASK-{row['task_id']}: {err}", file=sys.stderr)
        print(f"Reconciled {removed}/{len(eligible)} eligible worktrees.", file=sys.stderr)
        return 0
    finally:
        conn.close()


def _perform_relocate(
    conn: sqlite3.Connection,
    repo_root: str,
    row: dict,
    new_path: str,
) -> tuple[bool, list[str]]:
    """Run ``git worktree move`` then update the registry row.

    Returns ``(ok, errors)``. Fails fast on git failure — the registry row is
    left pointing at the old (still-valid) path so a retry has accurate state.
    """
    errors: list[str] = []
    parent = os.path.dirname(new_path)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        errors.append(f"could not create destination parent {parent}: {exc}")
        return False, errors

    result = _run_git(
        repo_root,
        ["worktree", "move", row["workspace_path"], new_path],
    )
    if result.returncode != 0:
        errors.append(
            f"git worktree move failed: "
            f"{(result.stderr.strip() or result.stdout.strip() or '(no output)')}"
        )
        return False, errors

    conn.execute(
        "UPDATE task_workspaces "
        "SET workspace_path = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (new_path, row["workspace_id"]),
    )
    conn.commit()
    return True, errors


def _classify_relocate_row(
    row: dict,
    workspace_root: str,
    namespace_dir: str,
) -> dict:
    """Decide whether ``row`` should be relocated, skipped, or is already namespaced.

    Returns ``{row, new_path, action, reason}``. ``action`` is one of
    ``"move"``, ``"skip"``, or ``"already_namespaced"``. Idempotency is
    enforced by the ``already_namespaced`` branch — a second relocate pass
    against the same registry never re-moves a workspace that landed in the
    target namespace dir on a prior run.
    """
    old_path = row["workspace_path"]
    slug_dir = os.path.basename(old_path.rstrip(os.sep)) or os.path.basename(old_path)
    new_path = os.path.join(namespace_dir, slug_dir)

    if os.path.normpath(old_path) == os.path.normpath(new_path):
        return {
            "row": row,
            "new_path": new_path,
            "action": "already_namespaced",
            "reason": "workspace path already matches the target namespace layout",
        }

    if not row["exists_on_disk"]:
        return {
            "row": row,
            "new_path": new_path,
            "action": "skip",
            "reason": f"workspace path missing on disk: {old_path}",
        }

    old_parent = os.path.dirname(old_path.rstrip(os.sep))
    if os.path.normpath(old_parent) != os.path.normpath(workspace_root):
        return {
            "row": row,
            "new_path": new_path,
            "action": "skip",
            "reason": (
                f"parent dir {old_parent!r} is not the configured workspace "
                f"root {workspace_root!r}"
            ),
        }

    if os.path.lexists(new_path):
        return {
            "row": row,
            "new_path": new_path,
            "action": "skip",
            "reason": f"destination already exists: {new_path}",
        }

    clean, dirty_detail = _worktree_is_clean(old_path)
    if not clean:
        detail = dirty_detail.strip() or "(unknown)"
        return {
            "row": row,
            "new_path": new_path,
            "action": "skip",
            "reason": f"worktree dirty: {detail}",
        }

    return {"row": row, "new_path": new_path, "action": "move", "reason": None}


def cmd_relocate(
    db_path: str, config_path: str, repo_root: str, argv: list[str]
) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree relocate",
        description=(
            "Migrate existing flat-pool task worktrees into the per-repo "
            "namespaced layout. Operates on the current repo's registry "
            "only — to migrate another repo's worktrees, run this command "
            "from inside that repo."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without moving anything or pruning stale rows.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip per-worktree confirmation prompts.",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help=(
            "Parent directory for task worktrees. Default: $TUSK_WORKTREE_ROOT "
            "or $HOME/.tusk/worktrees. Rows whose workspace_path lives "
            "directly under this root are candidates for relocation."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args(argv)

    workspace_root = (
        args.workspace_root
        or os.environ.get("TUSK_WORKTREE_ROOT")
        or os.path.join(os.path.expanduser("~"), ".tusk", "worktrees")
    )

    conn = get_connection(db_path)
    try:
        # Step 1: prune stale rows first (path missing on disk AND not in
        # `git worktree list`). Matches the predicate used by
        # `tusk task-worktree prune`. Honors --dry-run.
        pre_live = _parse_git_worktrees(repo_root)
        pre_rows = _list_workspaces_with_live_state(conn, pre_live)
        stale_rows = [r for r in pre_rows if _is_stale_workspace(r)]
        if stale_rows and not args.dry_run:
            conn.executemany(
                "DELETE FROM task_workspaces WHERE id = ?",
                [(r["workspace_id"],) for r in stale_rows],
            )
            conn.commit()
        pruned_count = len(stale_rows)

        # Step 2: compute the per-repo namespace for THIS repo. Skip the
        # marker write under --dry-run so planning never touches the FS.
        namespace = _namespace_for(
            workspace_root, repo_root, claim=not args.dry_run
        )
        namespace_dir = os.path.join(workspace_root, namespace)

        # Step 3: classify the remaining (non-stale) rows. Re-read live state
        # because the prune above mutated the registry.
        if args.dry_run:
            remaining = [r for r in pre_rows if not _is_stale_workspace(r)]
        else:
            remaining = _list_workspaces_with_live_state(
                conn, _parse_git_worktrees(repo_root)
            )
        plan = [
            _classify_relocate_row(r, workspace_root, namespace_dir)
            for r in remaining
        ]
        to_move = [p for p in plan if p["action"] == "move"]
        skipped = [p for p in plan if p["action"] == "skip"]
        already = [p for p in plan if p["action"] == "already_namespaced"]

        if args.format == "json":
            results: list[dict] = []
            if not args.dry_run:
                for entry in to_move:
                    ok, errors = _perform_relocate(
                        conn, repo_root, entry["row"], entry["new_path"]
                    )
                    results.append(
                        {
                            "task_id": entry["row"]["task_id"],
                            "branch": entry["row"]["branch"],
                            "old_path": entry["row"]["workspace_path"],
                            "new_path": entry["new_path"],
                            "ok": ok,
                            "errors": errors,
                        }
                    )
            print(
                dumps(
                    {
                        "dry_run": args.dry_run,
                        "workspace_root": workspace_root,
                        "namespace": namespace,
                        "pruned_count": pruned_count,
                        "plan": [
                            {
                                "task_id": p["row"]["task_id"],
                                "branch": p["row"]["branch"],
                                "old_path": p["row"]["workspace_path"],
                                "new_path": p["new_path"],
                                "action": p["action"],
                                "reason": p["reason"],
                            }
                            for p in plan
                        ],
                        "results": results,
                    }
                )
            )
            return 0

        # Text mode.
        if pruned_count:
            verb = "Would prune" if args.dry_run else "Pruned"
            print(
                f"{verb} {pruned_count} stale registry row(s) before relocate.",
                file=sys.stderr,
            )

        if not to_move:
            print(
                f"No worktrees to relocate (namespace: {namespace}).",
                file=sys.stderr,
            )
            for entry in already:
                print(
                    f"  ok TASK-{entry['row']['task_id']} branch="
                    f"{entry['row']['branch']}: already namespaced",
                    file=sys.stderr,
                )
            for entry in skipped:
                print(
                    f"  skip TASK-{entry['row']['task_id']} branch="
                    f"{entry['row']['branch']}: {entry['reason']}",
                    file=sys.stderr,
                )
            return 0

        print(
            f"Relocate plan ({len(to_move)} eligible, namespace: {namespace}):",
            file=sys.stderr,
        )
        for entry in to_move:
            print(
                f"  TASK-{entry['row']['task_id']} "
                f"{entry['row']['workspace_path']} -> {entry['new_path']}",
                file=sys.stderr,
            )
        if already:
            print(f"Already namespaced ({len(already)}):", file=sys.stderr)
            for entry in already:
                print(
                    f"  TASK-{entry['row']['task_id']} branch="
                    f"{entry['row']['branch']}",
                    file=sys.stderr,
                )
        if skipped:
            print(f"Skipped ({len(skipped)}):", file=sys.stderr)
            for entry in skipped:
                print(
                    f"  TASK-{entry['row']['task_id']}: {entry['reason']}",
                    file=sys.stderr,
                )

        if args.dry_run:
            print("Dry run — no changes made.", file=sys.stderr)
            return 0

        moved = 0
        for entry in to_move:
            if not args.yes:
                prompt = (
                    f"Move TASK-{entry['row']['task_id']} from "
                    f"{entry['row']['workspace_path']} to {entry['new_path']}? "
                    "[y/N] "
                )
                try:
                    answer = input(prompt).strip().lower()
                except EOFError:
                    answer = ""
                if answer not in {"y", "yes"}:
                    print(
                        f"  skipped TASK-{entry['row']['task_id']} (declined)",
                        file=sys.stderr,
                    )
                    continue
            ok, errors = _perform_relocate(
                conn, repo_root, entry["row"], entry["new_path"]
            )
            if ok:
                moved += 1
                print(
                    f"  moved TASK-{entry['row']['task_id']} -> "
                    f"{entry['new_path']}",
                    file=sys.stderr,
                )
            else:
                for err in errors:
                    print(
                        f"  error TASK-{entry['row']['task_id']}: {err}",
                        file=sys.stderr,
                    )
        print(
            f"Relocated {moved}/{len(to_move)} worktrees.",
            file=sys.stderr,
        )
        return 0
    finally:
        conn.close()


def cmd_prune(db_path: str, repo_root: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tusk task-worktree prune",
        description="Remove stale task-owned worktree registry rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview stale rows without deleting them.",
    )
    parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv)

    conn = get_connection(db_path)
    try:
        stale_rows = [
            row
            for row in _list_workspaces_with_live_state(
                conn, _parse_git_worktrees(repo_root)
            )
            if _is_stale_workspace(row)
        ]
        if stale_rows and not args.dry_run:
            conn.executemany(
                "DELETE FROM task_workspaces WHERE id = ?",
                [(row["workspace_id"],) for row in stale_rows],
            )
            conn.commit()
        print(
            dumps(
                {
                    "dry_run": args.dry_run,
                    "removed_count": len(stale_rows),
                    "removed": stale_rows,
                }
            )
        )
    finally:
        conn.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("Usage: tusk task-worktree list", file=sys.stderr)
        return 1

    db_path = argv[0]
    config_path = argv[1]
    repo_root = argv[2]
    command = argv[3] if len(argv) > 3 else ""
    rest = argv[4:]

    if command == "create":
        return cmd_create(db_path, config_path, repo_root, rest)
    if command in {"list", "status"}:
        return cmd_list(db_path, repo_root, rest)
    if command == "prune":
        return cmd_prune(db_path, repo_root, rest)
    if command == "reconcile":
        return cmd_reconcile(db_path, repo_root, rest)
    if command == "relocate":
        return cmd_relocate(db_path, config_path, repo_root, rest)

    print(
        "Usage: tusk task-worktree create <task_id> <slug> [--workspace-root <path>]\n"
        "       tusk task-worktree list [--format json]\n"
        "       tusk task-worktree prune [--dry-run] [--format json]\n"
        "       tusk task-worktree reconcile [--dry-run] [--yes] [--format text|json]\n"
        "       tusk task-worktree relocate [--dry-run] [--yes] [--workspace-root <path>] [--format text|json]",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
