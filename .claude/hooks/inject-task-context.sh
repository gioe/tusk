#!/bin/bash
# SessionStart hook: outputs a summary of in-progress tasks and their latest
# progress checkpoint so Claude starts every session aware of what is in flight.

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# Resolve tusk binary — PATH isn't set up yet during SessionStart hooks.
# Check source-repo path first, then installed path.
if [ -x "$REPO_ROOT/bin/tusk" ]; then
  TUSK="$REPO_ROOT/bin/tusk"
elif [ -x "$REPO_ROOT/.claude/bin/tusk" ]; then
  TUSK="$REPO_ROOT/.claude/bin/tusk"
else
  exit 0
fi

# Query in-progress tasks joined with their latest progress checkpoint
result=$("$TUSK" -json "
SELECT t.id, t.summary, t.complexity,
       p.commit_hash, p.next_steps, p.created_at AS progress_at
FROM tasks t
LEFT JOIN task_progress p ON p.task_id = t.id
  AND p.id = (SELECT MAX(p2.id) FROM task_progress p2 WHERE p2.task_id = t.id)
WHERE t.status = 'In Progress'
ORDER BY t.id;
" 2>/dev/null)

# No in-progress tasks → silent exit
if [ -z "$result" ] || [ "$result" = "[]" ]; then
  exit 0
fi

# Format a concise summary
ROWS="$result" python3 << 'PYEOF'
import os, json, sys

rows = json.loads(os.environ.get("ROWS", "[]"))
if not rows:
    sys.exit(0)

lines = ["=== Active Tasks ===", ""]
for r in rows:
    tid = r["id"]
    summary = r["summary"]
    complexity = r.get("complexity") or "?"
    commit = r.get("commit_hash") or None
    next_steps = r.get("next_steps") or None

    lines.append(f"TASK-{tid} [{complexity}]: {summary}")
    if commit:
        lines.append(f"  Last commit: {commit[:8]}")
    if next_steps:
        lines.append(f"  Next steps: {next_steps}")
    lines.append("")

print("\n".join(lines))
PYEOF

exit 0
