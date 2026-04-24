#!/bin/bash
# Git pre-commit guard: blocks commits that include an INSERT INTO tasks(...)
# statement whose summary collides with an existing task.
#
# Scans the staged diff for *added* lines in .sh / .py / .sql files. When an
# INSERT INTO tasks is detected, extracts the summary value and runs
# `tusk dupes check` against it. A non-zero exit from `tusk dupes` means a
# similar task already exists; this hook then rejects the commit.
#
# Exit 2 on violation, 0 otherwise. Bypass with `git commit --no-verify`.

set -u

added=$(git diff --cached --unified=0 --no-color --diff-filter=ACMR -- '*.sh' '*.py' '*.sql' 2>/dev/null | \
  awk '/^\+\+\+ / { next } /^\+/ { sub(/^\+/, ""); print }')

[ -z "$added" ] && exit 0

# Collapse added lines into a single string so multi-line INSERTs are seen
command=$(printf '%s\n' "$added")

# Quick check: nothing to do if no INSERT INTO tasks
echo "$command" | grep -qiE 'INSERT[[:space:]]+INTO[[:space:]]+tasks[[:space:]]*\(' || exit 0

# Use the same extraction logic as the PreToolUse dupe-gate hook
summary=$(HOOK_CMD="$command" python3 << 'PYEOF'
import os, re, sys

cmd = os.environ.get("HOOK_CMD", "")

m = re.search(r'INSERT\s+INTO\s+tasks\s*\(([^)]+)\)', cmd, re.IGNORECASE | re.DOTALL)
if not m:
    sys.exit(1)

cols = [c.strip() for c in m.group(1).split(',')]
try:
    idx = cols.index('summary')
except ValueError:
    sys.exit(1)

rest = cmd[m.end():]
vm = re.search(r'VALUES\s*\(', rest, re.IGNORECASE | re.DOTALL)
if not vm:
    sys.exit(1)

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

sm = re.search(r'\$\(tusk\s+sql-quote\s+"([^"]*)"\)', val)
if sm:
    print(sm.group(1))
    sys.exit(0)

sm = re.match(r"^'(.*)'$", val, re.DOTALL)
if sm:
    print(sm.group(1).replace("''", "'"))
    sys.exit(0)

sys.exit(1)
PYEOF
)
py_rc=$?

if [ "$py_rc" -ne 0 ] || [ -z "$summary" ]; then
  exit 0
fi

# Run the duplicate check; require tusk to be on PATH.
if ! command -v tusk >/dev/null 2>&1; then
  exit 0
fi

dupe_output=$(tusk dupes check "$summary" --json 2>&1)
dupe_rc=$?

if [ "$dupe_rc" -eq 1 ]; then
  echo "ERROR: duplicate task detected in staged INSERT INTO tasks:" >&2
  echo "  summary: $summary" >&2
  echo "" >&2
  echo "$dupe_output" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for d in data.get('duplicates', []):
        print(f\"  TASK-{d['id']} ({d['similarity']:.0%} match): {d['summary']}\")
except Exception:
    pass
" 2>/dev/null >&2
  echo "" >&2
  echo "Close the duplicate first, or bypass with: git commit --no-verify" >&2
  exit 2
fi

exit 0
