#!/usr/bin/env python3
"""Re-run the project_type filter and reconcile installed skills.

Skills declaring `applies_to_project_types` install only when the target's
`tusk/config.json:project_type` matches one of the listed types. install.sh
and `tusk upgrade` apply that filter at install time, but neither fires when
project_type *changes* on an already-installed project. This subcommand is
the explicit reconciliation surface — it re-evaluates the gating filter
against the current project_type and copies/removes skill directories under
`.claude/skills/` to match.

Behavior:
  - Locates a skills source: explicit `--source-dir`, or local
    `<repo_root>/skills/` (source-role installs), or — for consumer
    installs that have no source tree — downloads the latest GitHub
    tarball and uses its embedded `skills/` directory.
  - Reads project_type from <repo_root>/tusk/config.json.
  - For each gated skill in source:
      - If it should install and isn't currently in .claude/skills/, install it.
      - If it shouldn't install but currently is in .claude/skills/, remove it.
  - Universal (non-gated) skills are left alone — install.sh / upgrade install
    them unconditionally and they don't depend on project_type.

Source-role installs (tusk's own dev tree) use symlinks back to <repo_root>/skills/
so the layout matches `tusk sync-skills`. Consumer-role installs copy files in
to match install.sh.

CLI:
    tusk reconcile-skills [--source-dir <path>] [--dry-run] [--quiet] [--json]

Env overrides (testing):
    TUSK_RECONCILE_TARBALL_URL — fetch the tarball from this URL instead of
        the GitHub release endpoint. file:// URLs are honored. When set, the
        GitHub release-API call is skipped entirely.

Exit codes:
    0  success (or no changes — both report on stdout)
    2  could not locate skills source (and the tarball fetch fallback failed)
    3  not inside a git repository
"""

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import tusk_skill_filter as sf  # noqa: E402
import tusk_github as gh  # noqa: E402


def _find_local_source(repo_root: str) -> str | None:
    """Return path to <repo_root>/skills/ when it has at least one SKILL.md inside."""
    candidate = os.path.join(repo_root, "skills")
    if not os.path.isdir(candidate):
        return None
    for name in os.listdir(candidate):
        if os.path.isfile(os.path.join(candidate, name, "SKILL.md")):
            return candidate
    return None


def _fetch_remote_source(dest_dir: str) -> str | None:
    """Download the GitHub tarball into dest_dir and return the unpacked skills/ path.

    Resolves the tarball URL from `TUSK_RECONCILE_TARBALL_URL` if set (used by
    tests to point at a local file:// tarball), otherwise calls the GitHub
    release API for the latest tag and constructs the standard tarball URL.

    Returns None when the download or extract fails, or the tarball doesn't
    contain a skills/ tree under its top-level directory.
    """
    url = os.environ.get("TUSK_RECONCILE_TARBALL_URL")
    if not url:
        try:
            tag = gh.get_latest_tag()
        except SystemExit:
            return None
        url = gh.tarball_url(tag)

    tarball_path = os.path.join(dest_dir, "tusk-source.tar.gz")
    try:
        data = gh.fetch_bytes(url, timeout=gh.DL_TIMEOUT)
    except SystemExit:
        return None
    with open(tarball_path, "wb") as f:
        f.write(data)
    try:
        with tarfile.open(tarball_path) as tar:
            tar.extractall(dest_dir, filter="data")
    except (tarfile.TarError, OSError):
        return None

    # GitHub's archive layout puts everything under a single top-level dir
    # (e.g. tusk-v795/). Find the first subdirectory that contains skills/.
    for entry in sorted(os.listdir(dest_dir)):
        candidate = os.path.join(dest_dir, entry, "skills")
        if os.path.isdir(candidate):
            return candidate
    return None


def _detect_role(script_dir: str) -> str:
    """Read the install-mode marker; default 'source' (legacy/dev tree)."""
    marker = os.path.join(script_dir, "install-mode")
    if not os.path.isfile(marker):
        return "source"
    try:
        with open(marker, encoding="utf-8") as f:
            value = f.read().strip()
    except OSError:
        return "source"
    if "-" in value:
        return value.split("-", 1)[1]
    return "source"


def _resolve_repo_root() -> str | None:
    """Walk up from CWD (or TUSK_PROJECT) to the nearest .git entry.

    Matches `bin/tusk`'s `find_repo_root` semantics: `.git` may be a directory
    (normal clone) or a file (git worktree pointing at the real gitdir). Use
    `os.path.exists` to cover both, not `os.path.isdir`.
    """
    start = os.path.realpath(os.environ.get("TUSK_PROJECT") or os.getcwd())
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _install_skill_dir(src_dir: str, dst_dir: str, role: str) -> None:
    """Install/refresh .claude/skills/<name>/.

    Source-role: symlink (matches tusk sync-skills).
    Consumer-role: copy files.
    """
    if role == "source":
        if os.path.lexists(dst_dir):
            if os.path.islink(dst_dir):
                os.unlink(dst_dir)
            else:
                shutil.rmtree(dst_dir)
        os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
        target = os.path.relpath(src_dir, os.path.dirname(dst_dir))
        os.symlink(target, dst_dir)
    else:
        os.makedirs(dst_dir, exist_ok=True)
        for fname in os.listdir(src_dir):
            full = os.path.join(src_dir, fname)
            if os.path.isfile(full):
                shutil.copy2(full, dst_dir)


def _remove_skill_dir(dst_dir: str) -> None:
    if not os.path.lexists(dst_dir):
        return
    if os.path.islink(dst_dir):
        os.unlink(dst_dir)
    else:
        shutil.rmtree(dst_dir)


def reconcile(repo_root: str, source_dir: str, role: str, dry_run: bool) -> dict:
    project_type = sf.get_project_type(repo_root)
    claude_skills = os.path.join(repo_root, ".claude", "skills")
    os.makedirs(claude_skills, exist_ok=True)

    installed = []
    removed = []
    skipped_universal = []

    for skill_name in sorted(os.listdir(source_dir)):
        skill_src = os.path.join(source_dir, skill_name)
        if not os.path.isdir(skill_src):
            continue
        gates = sf.applies_to_project_types(skill_src)
        if gates is None:
            skipped_universal.append(skill_name)
            continue

        skill_dst = os.path.join(claude_skills, skill_name)
        currently_installed = os.path.lexists(skill_dst)
        should = sf.should_install_skill(skill_src, project_type)

        if should and not currently_installed:
            if not dry_run:
                _install_skill_dir(skill_src, skill_dst, role)
            installed.append(skill_name)
        elif not should and currently_installed:
            if not dry_run:
                _remove_skill_dir(skill_dst)
            removed.append(skill_name)

    return {
        "project_type": project_type,
        "installed": installed,
        "removed": removed,
        "skipped_universal": skipped_universal,
        "dry_run": dry_run,
    }


def _format_summary(result: dict) -> str:
    pt = result["project_type"] or "unset"
    suffix = " (dry run)" if result["dry_run"] else ""
    if not result["installed"] and not result["removed"]:
        return f"Skills already in sync (project_type={pt}){suffix}."
    lines = [f"Reconciled skills (project_type={pt}){suffix}:"]
    if result["installed"]:
        names = ", ".join(result["installed"])
        lines.append(f"  Installed {len(result['installed'])}: {names}")
    if result["removed"]:
        names = ", ".join(result["removed"])
        lines.append(f"  Removed {len(result['removed'])}: {names}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="tusk reconcile-skills")
    parser.add_argument(
        "--source-dir",
        default=None,
        help="Path to skills/ source. Defaults to <repo_root>/skills/ if present.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable summary",
    )
    args = parser.parse_args()

    repo_root = _resolve_repo_root()
    if repo_root is None:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(3)

    role = _detect_role(SCRIPT_DIR)
    if args.source_dir:
        source = args.source_dir
        if not os.path.isdir(source):
            print(f"Error: --source-dir does not exist: {source}", file=sys.stderr)
            sys.exit(2)
        fetch_tmpdir = None
    else:
        source = _find_local_source(repo_root)
        fetch_tmpdir = None
        if source is None:
            # Consumer installs have no <repo_root>/skills/ tree. Fall back to
            # downloading the GitHub tarball and using its embedded skills/ dir.
            # Force copy-mode regardless of role: the fetched tree is ephemeral,
            # so symlinks into it would dangle once we clean it up below.
            fetch_tmpdir = tempfile.mkdtemp(prefix="tusk-reconcile-")
            source = _fetch_remote_source(fetch_tmpdir)
            if source is None:
                shutil.rmtree(fetch_tmpdir, ignore_errors=True)
                print(
                    "Error: could not locate skills/ source. "
                    "Pass --source-dir <path>, or run `tusk upgrade` to refresh the source tree.",
                    file=sys.stderr,
                )
                sys.exit(2)
            role = "consumer"

    try:
        result = reconcile(repo_root, source, role, args.dry_run)
    finally:
        if fetch_tmpdir is not None:
            shutil.rmtree(fetch_tmpdir, ignore_errors=True)

    if args.json:
        print(json.dumps(result))
        return
    if args.quiet:
        return
    print(_format_summary(result))


if __name__ == "__main__":
    main()
