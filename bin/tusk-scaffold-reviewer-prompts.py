#!/usr/bin/env python3
"""Scaffold per-domain REVIEWER-PROMPT-<domain>.md files in .claude/skills/review-commits/.

For each domain in the project config, creates a stub file by prepending a
domain-specific focus comment to the base REVIEWER-PROMPT.md content.
Existing files are left untouched (idempotent). Skips gracefully if the
review-commits skill directory does not exist.

Usage:
    tusk-scaffold-reviewer-prompts.py <db_path> <config_path>

Output (JSON):
    {"created": ["REVIEWER-PROMPT-api.md", ...], "skipped": [...], "skill_dir_missing": false}
"""

import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("Usage: tusk-scaffold-reviewer-prompts.py <db_path> <config_path>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]

    # Resolve repo root from db_path (.../tusk/tasks.db → repo root)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))

    # Load config to get domains
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading config: {e}", file=sys.stderr)
        sys.exit(1)

    domains = config.get("domains", [])
    if not domains:
        print(json.dumps({"created": [], "skipped": [], "skill_dir_missing": False}))
        return

    skill_dir = os.path.join(repo_root, ".claude", "skills", "review-commits")
    if not os.path.isdir(skill_dir):
        print(json.dumps({"created": [], "skipped": [], "skill_dir_missing": True}))
        return

    base_prompt_path = os.path.join(skill_dir, "REVIEWER-PROMPT.md")
    if not os.path.isfile(base_prompt_path):
        print(f"Base REVIEWER-PROMPT.md not found at {base_prompt_path}", file=sys.stderr)
        sys.exit(1)

    with open(base_prompt_path) as f:
        base_content = f.read()

    created = []
    skipped = []

    for domain in domains:
        filename = f"REVIEWER-PROMPT-{domain}.md"
        dest_path = os.path.join(skill_dir, filename)

        if os.path.isfile(dest_path):
            skipped.append(filename)
            continue

        stub_content = (
            f"# Domain: {domain} \u2014 customize this prompt for {domain}-specific review concerns\n\n"
            + base_content
        )
        with open(dest_path, "w") as f:
            f.write(stub_content)
        created.append(filename)

    print(json.dumps({"created": created, "skipped": skipped, "skill_dir_missing": False}))


if __name__ == "__main__":
    main()
