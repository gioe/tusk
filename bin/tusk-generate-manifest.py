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
from tusk_underscore_bin_files import (  # noqa: E402
    UNDERSCORE_BIN_FILES,
    get_underscore_bin_files,
)


def get_repo_root():
    """Resolve the repo root by walking up from this script's own location.

    Issue #882: the previous implementation invoked ``git rev-parse
    --show-toplevel`` against ``$PWD``, which silently picks the wrong repo
    when the script is invoked by absolute path from a sibling repo's CWD.
    The failure mode is a stale MANIFEST that omits any new ``bin/tusk-*.py``
    file added to the worktree the operator intended to enumerate — the
    on-disk walk runs against primary's bin/ instead, and the user-facing
    "Wrote MANIFEST ... (no changes)" output is indistinguishable from a
    clean run.

    ``__file__`` always resolves to this script's location on disk
    regardless of the caller's CWD, so ``dirname(dirname(abspath(__file__)))``
    is the repo root containing this script's own ``bin/`` directory — the
    repo the operator intended to enumerate. Mirrors the pattern
    ``bin/tusk-resolve-schema-bin.py`` uses for its own caller-relative
    repo root derivation. The source-repo guard in ``main()`` (refusal when
    ``bin/tusk`` is absent at the resolved root) still surfaces an
    actionable error when the script is somehow invoked from outside a
    source-repo layout.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _tracked_paths(root):
    """Repo-relative paths of every tracked file at ``root``.

    ``git ls-files`` reads the index, so it lists every committed/staged file
    regardless of whether it is materialized on disk. That is exactly what makes
    it safe under sparse-checkout: out-of-cone files are absent from the working
    tree but still present in the index, so enumerating from this list cannot
    silently drop them (the failure mode the on-disk walk has — issue #1125).

    Returns ``None`` when git is unavailable or the command fails, so callers can
    fall back to the conservative refusal rather than emit a partial MANIFEST.
    """
    result = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def _disk_lister(root):
    """Source the source-tree categories from the on-disk working tree.

    This is the proven non-sparse path: ``glob``/``os.listdir`` over the
    materialized tree. Reproduces the exact pre-#1125 behaviour so the
    primary-checkout output is unchanged.
    """
    def list_subdirs(reldir):
        base = os.path.join(root, reldir)
        if not os.path.isdir(base):
            return []
        # Sort by ``name + "/"`` to replicate the historical ``sorted(glob(
        # "skills/*/"))`` ordering: glob yields trailing-slash paths, so a name
        # that is a prefix of another (e.g. "investigate" vs
        # "investigate-directory") sorts AFTER it ("-" < "/"). Preserves
        # byte-identical MANIFEST output vs the pre-refactor walk.
        return sorted(
            (d for d in os.listdir(base)
             if os.path.isdir(os.path.join(base, d))),
            key=lambda d: d + "/",
        )

    def list_files(reldir):
        base = os.path.join(root, reldir)
        if not os.path.isdir(base):
            return []
        return sorted(
            f for f in os.listdir(base)
            if os.path.isfile(os.path.join(base, f))
        )

    def glob_bin():
        return sorted(
            os.path.basename(p)
            for p in glob.glob(os.path.join(root, "bin", "tusk-*.py"))
        )

    def underscore_files():
        return get_underscore_bin_files(root)

    return list_subdirs, list_files, glob_bin, underscore_files


def _git_lister(root):
    """Source the source-tree categories from ``git ls-files`` (sparse-safe).

    Each primitive mirrors its on-disk counterpart's semantics exactly:
    ``list_files`` is non-recursive (direct children only, matching
    ``os.listdir`` + ``os.path.isfile``); ``list_subdirs`` returns the immediate
    child directory names; ``glob_bin`` matches top-level ``bin/tusk-*.py`` only.
    Returns ``None`` when the tracked-file list cannot be read.
    """
    tracked = _tracked_paths(root)
    if tracked is None:
        return None

    def list_subdirs(reldir):
        prefix = reldir.rstrip("/") + "/"
        subs = set()
        for p in tracked:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                if "/" in rest:
                    subs.add(rest.split("/", 1)[0])
        # Match _disk_lister's trailing-slash sort key so the git path produces
        # byte-identical output to the on-disk walk (parity, criterion 3).
        return sorted(subs, key=lambda d: d + "/")

    def list_files(reldir):
        prefix = reldir.rstrip("/") + "/"
        out = []
        for p in tracked:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                if rest and "/" not in rest:
                    out.append(rest)
        return sorted(out)

    def glob_bin():
        out = []
        for p in tracked:
            if p.startswith("bin/"):
                rest = p[len("bin/"):]
                if "/" not in rest and rest.startswith("tusk-") and rest.endswith(".py"):
                    out.append(rest)
        return sorted(out)

    def underscore_files():
        # Canonical list ∩ tracked, so an out-of-cone underscore file is still
        # enumerated (get_underscore_bin_files filters by on-disk existence,
        # which would drop it under a bin-excluded cone).
        tracked_set = set(tracked)
        return [name for name in UNDERSCORE_BIN_FILES
                if ("bin/" + name) in tracked_set]

    return list_subdirs, list_files, glob_bin, underscore_files


def _enumerate(root, lister):
    """Map the source tree to install paths using ``lister``'s primitives.

    The single source of truth for the path mapping — both the on-disk and the
    git-ls-files paths run through here, so the two enumerations cannot drift.
    """
    list_subdirs, list_files, glob_bin, underscore_files = lister

    files = []

    files.append(".claude/bin/tusk")

    for base in glob_bin():
        if base in _DIST_EXCLUDED:
            continue
        files.append(".claude/bin/" + base)

    # Underscore-named bin/ files — canonical list lives in bin/tusk_underscore_bin_files.py.
    for name in underscore_files():
        files.append(".claude/bin/" + name)

    for name in ["config.default.json", "VERSION", "pricing.json"]:
        files.append(".claude/bin/" + name)

    for skill_name in list_subdirs("skills"):
        for fname in list_files("skills/" + skill_name):
            files.append(".claude/skills/" + skill_name + "/" + fname)

    for fname in list_files(".claude/hooks"):
        files.append(".claude/hooks/" + fname)

    for fname in list_files("hooks/git"):
        files.append(".claude/bin/hooks/git/" + fname)

    # Codex-only prompts. The tarball MANIFEST always lists the canonical
    # tarball-shaped path; translate_manifest_for_mode() in tusk-upgrade.py
    # drops these entries in claude mode and keeps them in codex mode.
    for fname in list_files("codex-prompts"):
        if fname.endswith(".md"):
            files.append(".codex/prompts/" + fname)

    return files


def build_manifest(root):
    # Under sparse-checkout the on-disk source-tree walk silently omits every
    # file outside the cone — regenerating MANIFEST from that partial view would
    # drop entries for unmaterialized hooks/skills/codex-prompts and corrupt
    # ``tusk upgrade`` for downstream installs (TASK-480, issues #895 / #905).
    # Rather than refuse (the original gate), source the complete tracked-file
    # set from ``git ls-files`` — which reads the index and is complete even
    # under sparse — so new-skill/new-script tasks can regenerate MANIFEST from
    # their sparse task worktree without first running ``git sparse-checkout
    # disable`` (issue #1125). The working tree's sparse state is never mutated.
    # The non-sparse primary checkout keeps the proven on-disk walk unchanged.
    # If the tracked-file list cannot be read (git unavailable), fall back to
    # the conservative refusal — never emit a partial MANIFEST.
    if _sparse_checkout_active(root):
        lister = _git_lister(root)
        if lister is None:
            print(
                "Error: tusk generate-manifest is running under a sparse "
                "worktree but could not read the tracked-file list via "
                "`git ls-files` to enumerate completely.\n"
                f"  Worktree: {root}\n"
                "  Recover: run from the primary checkout (a full checkout of "
                "the source repo) or run `git sparse-checkout disable` in this "
                "worktree first, then retry.",
                file=sys.stderr,
            )
            sys.exit(1)
        return _enumerate(root, lister)

    return _enumerate(root, _disk_lister(root))


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
