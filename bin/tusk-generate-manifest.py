#!/usr/bin/env python3
"""Regenerate MANIFEST from the current source tree.

Uses the same enumeration logic as rule18_manifest_drift (which mirrors
install.sh section 4c) to enumerate all files that install.sh distributes to
a target project, then writes the sorted JSON array to MANIFEST in the repo
root.
"""

import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tusk_underscore_bin_files import get_underscore_bin_files  # noqa: E402


def get_repo_root():
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def _sparse_checkout_active(root):
    """Return True when sparse-checkout is enabled in the worktree at ``root``.

    Reading the on-disk source tree from a sparse worktree silently omits
    every file outside the cone, so regenerating MANIFEST from that partial
    view destroys the entries for unmaterialized files — the issue #895
    (silent data corruption) / #905 (post-merge MANIFEST left dirty) cluster
    that TASK-480 (criterion 2228) closes by refusing to write rather than
    walking via ``git ls-files`` (which would force the operator to widen
    the cone first; the refusal is the more honest signal).
    """
    result = subprocess.run(
        ["git", "-C", root, "config", "--get", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


# Canonical source: bin/dist-excluded.txt — install.sh and tusk-lint.py read from the same file.
def _load_dist_excluded():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "dist-excluded.txt"), encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


_DIST_EXCLUDED = _load_dist_excluded()


def build_manifest(root):
    # Refuse under sparse-checkout to prevent silent MANIFEST destruction
    # (TASK-480 criterion 2228, issues #895 / #905; defense-in-depth move
    # from main() per issue #909). The on-disk source-tree walk below
    # silently omits every file outside the sparse cone — a regenerated
    # MANIFEST would drop ~17+ entries for unmaterialized hooks, skills,
    # codex-prompts, etc. The downstream impact is high: ``tusk upgrade``
    # for consumers would stop distributing the missing files. The gate
    # lives at this lowest-level write point so any direct caller (Python
    # import or future subprocess wrapper) is protected — not only the
    # ``main()`` entry point.
    if _sparse_checkout_active(root):
        print(
            "Error: tusk generate-manifest refuses to run under a sparse "
            "worktree — every file outside the cone would be silently dropped "
            "from MANIFEST, corrupting tusk upgrade for downstream installs.\n"
            f"  Worktree: {root}\n"
            "  Recover: run from the primary checkout (a full checkout of "
            "the source repo) or run `git sparse-checkout disable` in this "
            "worktree first, then retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    files = []

    files.append(".claude/bin/tusk")

    for p in sorted(glob.glob(os.path.join(root, "bin", "tusk-*.py"))):
        if os.path.basename(p) in _DIST_EXCLUDED:
            continue
        files.append(".claude/bin/" + os.path.basename(p))

    # Underscore-named bin/ files — canonical list lives in bin/tusk_underscore_bin_files.py.
    for name in get_underscore_bin_files(root):
        files.append(".claude/bin/" + name)

    for name in ["config.default.json", "VERSION", "pricing.json"]:
        files.append(".claude/bin/" + name)

    for skill_dir in sorted(glob.glob(os.path.join(root, "skills", "*/"))):
        skill_name = os.path.basename(skill_dir.rstrip("/"))
        for fname in sorted(os.listdir(skill_dir)):
            full = os.path.join(skill_dir, fname)
            if os.path.isfile(full):
                files.append(".claude/skills/" + skill_name + "/" + fname)

    hooks_src = os.path.join(root, ".claude", "hooks")
    if os.path.isdir(hooks_src):
        for fname in sorted(os.listdir(hooks_src)):
            full = os.path.join(hooks_src, fname)
            if os.path.isfile(full):
                files.append(".claude/hooks/" + fname)

    git_hooks_src = os.path.join(root, "hooks", "git")
    if os.path.isdir(git_hooks_src):
        for fname in sorted(os.listdir(git_hooks_src)):
            full = os.path.join(git_hooks_src, fname)
            if os.path.isfile(full):
                files.append(".claude/bin/hooks/git/" + fname)

    # Codex-only prompts. The tarball MANIFEST always lists the canonical
    # tarball-shaped path; translate_manifest_for_mode() in tusk-upgrade.py
    # drops these entries in claude mode and keeps them in codex mode.
    prompts_src = os.path.join(root, "codex-prompts")
    if os.path.isdir(prompts_src):
        for fname in sorted(os.listdir(prompts_src)):
            full = os.path.join(prompts_src, fname)
            if os.path.isfile(full) and fname.endswith(".md"):
                files.append(".codex/prompts/" + fname)

    return files


def main():
    root = get_repo_root()

    if not os.path.isfile(os.path.join(root, "bin", "tusk")):
        print("Error: this command must be run inside the tusk source repo", file=sys.stderr)
        sys.exit(1)

    # Sparse-checkout refusal lives at the top of build_manifest() so every
    # caller is protected, not just this entry point (issue #909).

    manifest_path = os.path.join(root, "MANIFEST")
    tusk_manifest_path = os.path.join(root, ".claude", "tusk-manifest.json")

    # Load existing manifest to compute diff for the summary
    old_entries = set()
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                old_entries = set(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass

    new_entries = build_manifest(root)
    new_set = set(new_entries)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(new_entries, f, indent=2)
        f.write("\n")

    with open(tusk_manifest_path, "w", encoding="utf-8") as f:
        json.dump(new_entries, f, indent=2)
        f.write("\n")

    added = sorted(new_set - old_entries)
    removed = sorted(old_entries - new_set)

    print(f"Wrote MANIFEST and .claude/tusk-manifest.json ({len(new_entries)} entries)")
    if added:
        for path in added:
            print(f"  + {path}")
    if removed:
        for path in removed:
            print(f"  - {path}")
    if not added and not removed:
        print("  (no changes)")


if __name__ == "__main__":
    main()
