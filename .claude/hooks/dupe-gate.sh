#!/bin/bash
# PreToolUse hook: blocks INSERT INTO tasks when a duplicate summary exists.
# Extracts the summary from the SQL, runs tusk dupes check, exits 2 on match.

# Read JSON from stdin
input=$(cat)

# Extract the command from tool_input.command
command=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('command', ''))
" 2>/dev/null)

# Quick check: skip if not an INSERT INTO tasks
echo "$command" | grep -qiE 'INSERT[[:space:]]+INTO[[:space:]]+tasks[[:space:]]*\(' || exit 0

# Extract the summary value from the INSERT statement
summary=$(HOOK_CMD="$command" python3 << 'PYEOF'
import os, re, sys

cmd = os.environ.get("HOOK_CMD", "")

# Extract column list from INSERT INTO tasks (col1, col2, ...)
m = re.search(r'INSERT\s+INTO\s+tasks\s*\(([^)]+)\)', cmd, re.IGNORECASE | re.DOTALL)
if not m:
    sys.exit(1)

cols = [c.strip() for c in m.group(1).split(',')]
try:
    idx = cols.index('summary')
except ValueError:
    sys.exit(1)

# Find VALUES clause
rest = cmd[m.end():]
vm = re.search(r'VALUES\s*\(', rest, re.IGNORECASE | re.DOTALL)
if not vm:
    sys.exit(1)

# Parse value list respecting quotes and parentheses
vstr = rest[vm.end():]
values = []
buf = []
depth = 0
in_sq = False
in_dq = False
i = 0

while i < len(vstr):
    ch = vstr[i]
    if ch == '\\' and i + 1 < len(vstr) and not in_sq:
        buf.append(ch + vstr[i + 1])
        i += 2
        continue
    if ch == "'" and not in_dq:
        in_sq = not in_sq
    elif ch == '"' and not in_sq:
        in_dq = not in_dq
    elif not in_sq and not in_dq:
        if ch == '(':
            depth += 1
        elif ch == ')':
            if depth == 0:
                values.append(''.join(buf).strip())
                break
            depth -= 1
        elif ch == ',' and depth == 0:
            values.append(''.join(buf).strip())
            buf = []
            i += 1
            continue
    buf.append(ch)
    i += 1

if idx >= len(values):
    sys.exit(1)

val = values[idx]

# Pattern 1: $(tusk sql-quote "...")
sm = re.search(r'\$\(tusk\s+sql-quote\s+"([^"]*)"\)', val)
if sm:
    print(sm.group(1))
    sys.exit(0)

# Pattern 2: Single-quoted SQL string '...'
sm = re.match(r"^'(.*)'$", val, re.DOTALL)
if sm:
    print(sm.group(1).replace("''", "'"))
    sys.exit(0)

sys.exit(1)
PYEOF
)
py_rc=$?

# If extraction failed or no summary found, allow the command
if [ "$py_rc" -ne 0 ] || [ -z "$summary" ]; then
  exit 0
fi

# Run duplicate check
dupe_output=$(tusk dupes check "$summary" --json 2>&1)
dupe_rc=$?

# Exit code 1 from tusk dupes = duplicates found
if [ "$dupe_rc" -eq 1 ]; then
  echo "Duplicate task detected! Similar tasks found for summary:"
  echo "  '$summary'"
  echo ""
  echo "$dupe_output" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for d in data.get('duplicates', []):
        print(f\"  TASK-{d['id']} ({d['similarity']:.0%} match): {d['summary']}\")
except:
    pass
" 2>/dev/null
  echo ""
  echo "Run /check-dupes to review, or close the duplicate first."
  exit 2
fi

exit 0
