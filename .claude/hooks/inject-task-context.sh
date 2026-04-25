#!/bin/bash
# SessionStart hook: emits a compact one-line JSON summary of in-progress tasks.
# Set TUSK_NO_SESSION_CONTEXT=1 to disable injection entirely.

[ "$TUSK_NO_SESSION_CONTEXT" = "1" ] && exit 0

# Resolve REPO_ROOT and TUSK via the shared helper. PATH isn't set up yet
# during SessionStart hooks, so the helper's command-v fallback usually
# misses — but the in-repo paths cover both source and installed layouts.
source "$(dirname "$0")/hook-common.sh"

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
