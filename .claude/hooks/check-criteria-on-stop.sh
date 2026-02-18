#!/bin/bash
# Stop hook: warns about in-progress tasks with incomplete acceptance criteria.
# Advisory only — always exits 0 so it never blocks Claude from stopping.

# Query for in-progress tasks that have at least one incomplete criterion
result=$(tusk -json "
SELECT t.id, t.summary,
       COUNT(ac.id) AS total_criteria,
       SUM(CASE WHEN ac.is_completed = 0 THEN 1 ELSE 0 END) AS incomplete
FROM tasks t
JOIN acceptance_criteria ac ON ac.task_id = t.id
WHERE t.status = 'In Progress'
GROUP BY t.id
HAVING incomplete > 0
ORDER BY t.id;
" 2>/dev/null)

# If query failed or returned empty, nothing to warn about
if [ -z "$result" ] || [ "$result" = "[]" ]; then
  exit 0
fi

# Format the warning
warning=$(ROWS="$result" python3 << 'PYEOF'
import os, json, sys

rows = json.loads(os.environ.get("ROWS", "[]"))
if not rows:
    sys.exit(0)

lines = ["WARNING: The following in-progress tasks have incomplete acceptance criteria:", ""]
for r in rows:
    tid = r["id"]
    summary = r["summary"]
    incomplete = r["incomplete"]
    total = r["total_criteria"]
    lines.append(f"  TASK-{tid}: {summary} ({incomplete}/{total} criteria incomplete)")

lines.append("")
lines.append("Consider addressing unfinished criteria before ending the session,")
lines.append("or log a progress checkpoint so the next session knows where to resume.")
print("\n".join(lines))
PYEOF
)

if [ -n "$warning" ]; then
  echo "$warning"
fi

exit 0
