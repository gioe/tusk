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


def get_repo_root():
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


# Canonical source: bin/dist-excluded.txt — install.sh and tusk-lint.py read from the same file.
def _load_dist_excluded():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "dist-excluded.txt"), encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


_DIST_EXCLUDED = _load_dist_excluded()


def build_manifest(root):
    files = []

    files.append(".claude/bin/tusk")

    for p in sorted(glob.glob(os.path.join(root, "bin", "tusk-*.py"))):
        if os.path.basename(p) in _DIST_EXCLUDED:
            continue
        files.append(".claude/bin/" + os.path.basename(p))

    # tusk_loader.py uses an underscore filename — not matched by the tusk-*.py glob above.
    if os.path.isfile(os.path.join(root, "bin", "tusk_loader.py")):
        files.append(".claude/bin/tusk_loader.py")

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

    return files


def main():
    root = get_repo_root()

    if not os.path.isfile(os.path.join(root, "bin", "tusk")):
        print("Error: this command must be run inside the tusk source repo", file=sys.stderr)
        sys.exit(1)

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
