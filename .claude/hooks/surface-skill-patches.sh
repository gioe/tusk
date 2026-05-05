#!/bin/bash
# SessionStart hook: surfaces unconfirmed `skill-patch:<file>` retro_findings
# whose target_file corresponds to a skill (or CLAUDE.md/AGENTS.md) loaded
# in this session, so the agent can validate whether the patch held and
# file a `skill-patch-confirmed:<file>` retro_finding via /retro to close
# the feedback loop the patch opened.
#
# Set TUSK_NO_SKILL_PATCH_NOTICE=1 to disable.
# Set TUSK_SKILL_PATCH_WINDOW_DAYS=<N> to override the 30-day window.

[ "$TUSK_NO_SKILL_PATCH_NOTICE" = "1" ] && exit 0

source "$(dirname "$0")/hook-common.sh"

WINDOW="${TUSK_SKILL_PATCH_WINDOW_DAYS:-30}"

result=$("$TUSK" retro-patches --window-days "$WINDOW" --unconfirmed 2>/dev/null)

# Empty list → silent exit
if [ -z "$result" ] || [ "$result" = "[]" ]; then
  exit 0
fi

ROWS="$result" REPO_ROOT="$REPO_ROOT" python3 << 'PYEOF'
import json
import os
import sys
from pathlib import Path

repo_root = Path(os.environ.get("REPO_ROOT", "."))
rows = json.loads(os.environ.get("ROWS", "[]"))
if not rows:
    sys.exit(0)

# "Loaded" means available to load from this repo: every skill on disk
# under .claude/skills/<name>/SKILL.md plus the always-read CLAUDE.md /
# AGENTS.md at the repo root. SessionStart fires before any skill is
# actually invoked, so we approximate with on-disk availability.
loaded = set()
skills_dir = repo_root / ".claude" / "skills"
if skills_dir.is_dir():
    for child in skills_dir.iterdir():
        if (child / "SKILL.md").exists():
            loaded.add(f"skills/{child.name}/SKILL.md")
for top in ("CLAUDE.md", "AGENTS.md"):
    if (repo_root / top).exists():
        loaded.add(top)

if not loaded:
    sys.exit(0)

# target_file may be comma-separated for compound skill-patches that
# touched several files in one retro pass (e.g. tusk + chain together).
matches = []
for r in rows:
    tf = r.get("target_file") or ""
    files = [f.strip() for f in tf.split(",") if f.strip()]
    hit = [f for f in files if f in loaded]
    if hit:
        matches.append((r, hit))

if not matches:
    sys.exit(0)

parts = []
for r, hit in matches:
    files_str = ",".join(hit)
    parts.append(f"{files_str} (TASK-{r['task_id']}, {r['age_days']}d ago)")
joined = "; ".join(parts)
print(
    f"Unconfirmed skill-patches awaiting confirmation: {joined}. "
    "If you observe one of these patched behaviors holding this session, "
    "file a `skill-patch-confirmed:<file>` retro_finding via /retro to close the feedback loop."
)
PYEOF

exit 0
