#!/usr/bin/env python3
"""Filter skills by project_type during install/upgrade.

Skills can declare an `applies_to_project_types` frontmatter field
(YAML flow-style list, e.g. `[ios_app, android_app]`). Skills with
the field install only when the target project's `tusk/config.json`
`project_type` matches one of the listed types. Skills without the
field stay universal and always install.

When project_type is unset (no `tusk/config.json` yet, or the field
is null), gated skills are deferred — they will install on a future
upgrade once /tusk-init writes a project_type.

CLI:
    tusk_skill_filter.py --skill <skill_dir> --project-type <pt>

Exits 0 when the skill should install, 1 when it should be skipped.
An empty --project-type means the field is unset; gated skills are
deferred.
"""

import json
import os
import re
import sys


_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def parse_skill_frontmatter(skill_md_path):
    """Parse minimal YAML frontmatter from a SKILL.md.

    Supports `key: value` (string scalar) and `key: [a, b]` (flow-style
    list). Block-style lists (`- item` on subsequent lines) are not
    supported — gated frontmatter must use flow style.
    Returns an empty dict if the file is missing, has no frontmatter,
    or the frontmatter is unclosed.
    """
    try:
        with open(skill_md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fm_lines = []
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        fm_lines.append(line)
    if not closed:
        return {}

    fm = {}
    for line in fm_lines:
        m = _FRONTMATTER_KEY_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [item.strip().strip("\"'") for item in inner.split(",")]
        else:
            fm[key] = raw.strip("\"'")
    return fm


def applies_to_project_types(skill_dir):
    """Return list[str] | None — None means universal (always install)."""
    skill_md = os.path.join(skill_dir, "SKILL.md")
    fm = parse_skill_frontmatter(skill_md)
    v = fm.get("applies_to_project_types")
    if v is None:
        return None
    if isinstance(v, list):
        return v
    return [v]


def should_install_skill(skill_dir, project_type):
    """Decide whether a skill should ship to a project with the given project_type.

    Universal skills (no `applies_to_project_types`) always install.
    Gated skills install only when project_type is a non-empty string
    listed in the frontmatter.
    """
    gates = applies_to_project_types(skill_dir)
    if gates is None:
        return True
    return bool(project_type) and project_type in gates


def get_project_type(repo_root):
    """Read project_type from <repo_root>/tusk/config.json. Returns str | None."""
    cfg = os.path.join(repo_root, "tusk", "config.json")
    if not os.path.isfile(cfg):
        return None
    try:
        with open(cfg, encoding="utf-8") as f:
            return json.load(f).get("project_type")
    except (OSError, json.JSONDecodeError):
        return None


def filter_manifest(files, skills_src, project_type):
    """Drop `.claude/skills/<name>/*` entries whose skill is gated to a
    project_type that doesn't match the target's project_type.

    `files` is the list of manifest paths; `skills_src` is the directory
    holding the skill source dirs (so we can read SKILL.md frontmatter).
    Non-skill paths pass through unchanged.
    """
    if not os.path.isdir(skills_src):
        return list(files)

    decisions = {}
    for skill_name in os.listdir(skills_src):
        skill_dir = os.path.join(skills_src, skill_name)
        if os.path.isdir(skill_dir):
            decisions[skill_name] = should_install_skill(skill_dir, project_type)

    out = []
    for path in files:
        if path.startswith(".claude/skills/"):
            parts = path.split("/", 3)
            if len(parts) >= 3:
                skill_name = parts[2]
                if decisions.get(skill_name, True) is False:
                    continue
        out.append(path)
    return out


def _cli():
    import argparse
    p = argparse.ArgumentParser(
        description="Filter a skill by project_type. Exit 0 to install, 1 to skip."
    )
    p.add_argument("--skill", required=True, help="Path to skill directory containing SKILL.md")
    p.add_argument("--project-type", default="", help="Current project_type (empty for unset)")
    args = p.parse_args()
    pt = args.project_type or None
    sys.exit(0 if should_install_skill(args.skill, pt) else 1)


if __name__ == "__main__":
    _cli()
