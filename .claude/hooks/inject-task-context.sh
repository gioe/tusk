#!/bin/bash
# SessionStart hook: emits a compact one-line JSON summary of in-progress tasks.
# Set TUSK_NO_SESSION_CONTEXT=1 to disable injection entirely.

[ "$TUSK_NO_SESSION_CONTEXT" = "1" ] && exit 0

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

result=$("$TUSK" -json "
SELECT t.id, t.summary, t.complexity
FROM tasks t
WHERE t.status = 'In Progress'
ORDER BY t.priority_score DESC, t.id;
" 2>/dev/null)

# No in-progress tasks → silent exit
if [ -z "$result" ] || [ "$result" = "[]" ]; then
  exit 0
fi

ROWS="$result" python3 << 'PYEOF'
import os, json, sys

rows = json.loads(os.environ.get("ROWS", "[]"))
if not rows:
    sys.exit(0)

def _trunc(s, n):
    s = s or ""
    if len(s) <= n:
        return s
    return s[: n - 3].rstrip() + "..."

def _entry(r, max_s):
    return {
        "id": r["id"],
        "c": r.get("complexity") or "?",
        "s": _trunc(r["summary"], max_s),
    }

if len(rows) > 3:
    out = {"in_progress_count": len(rows), "top": _entry(rows[0], 60)}
else:
    out = {"in_progress": [_entry(r, 32) for r in rows]}
print(json.dumps(out, separators=(",", ":")))
PYEOF

exit 0
