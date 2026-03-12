#!/usr/bin/env python3
"""Regenerate .claude/skills/ symlinks from skills/ and skills-internal/.

Removes all existing symlinks in .claude/skills/, then creates one symlink
per skill directory found in skills/ (public) and skills-internal/ (private).
Internal skills take precedence: if the same skill name exists in both
directories, the skills-internal/ version wins and a warning is printed.
"""

import os
import subprocess
import sys


def get_repo_root():
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def main():
    repo_root = get_repo_root()
    claude_skills = os.path.join(repo_root, ".claude", "skills")
    public_dir = os.path.join(repo_root, "skills")
    internal_dir = os.path.join(repo_root, "skills-internal")

    # Ensure .claude/skills/ exists as a real directory
    if os.path.islink(claude_skills):
        os.unlink(claude_skills)
    os.makedirs(claude_skills, exist_ok=True)

    # Remove existing symlinks (but not real files/dirs — those shouldn't be there)
    for entry in os.listdir(claude_skills):
        path = os.path.join(claude_skills, entry)
        if os.path.islink(path):
            os.unlink(path)

    created = []

    # Link internal skills first: .claude/skills/<name> -> ../../skills-internal/<name>
    # Internal skills take precedence over public skills of the same name.
    if os.path.isdir(internal_dir):
        for name in sorted(os.listdir(internal_dir)):
            skill_path = os.path.join(internal_dir, name)
            if not os.path.isdir(skill_path):
                continue
            link = os.path.join(claude_skills, name)
            target = os.path.join("..", "..", "skills-internal", name)
            os.symlink(target, link)
            created.append(("internal", name))

    # Link public skills: .claude/skills/<name> -> ../../skills/<name>
    # Skip any name already claimed by an internal skill.
    if os.path.isdir(public_dir):
        for name in sorted(os.listdir(public_dir)):
            skill_path = os.path.join(public_dir, name)
            if not os.path.isdir(skill_path):
                continue
            link = os.path.join(claude_skills, name)
            if os.path.exists(link):
                print(f"  Warning: '{name}' exists in both skills/ and skills-internal/ — internal version wins", file=sys.stderr)
                continue
            target = os.path.join("..", "..", "skills", name)
            os.symlink(target, link)
            created.append(("public", name))

    for source, name in created:
        print(f"  {name} ({source})")

    print(f"\nSynced {len(created)} skill(s) into .claude/skills/")


if __name__ == "__main__":
    main()
