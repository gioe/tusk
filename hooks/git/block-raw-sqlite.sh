#!/bin/bash
# Git pre-commit guard: blocks commits that introduce raw `sqlite3` CLI
# invocations. All DB access should go through bin/tusk.
#
# Scans the staged diff for *added* lines in .sh / .py files that invoke
# sqlite3 in command position (start-of-line, after `|`, `;`, `&&`, `||`, or
# `$()`). Quoted string literals on the added line are stripped first so that
# e.g. `echo "use sqlite3 instead"` does not trip the guard.
#
# Exit 2 on violation, 0 otherwise. Bypass with `git commit --no-verify`.

set -u

added=$(git diff --cached --unified=0 --no-color --diff-filter=ACMR -- '*.sh' '*.py' 2>/dev/null | \
  awk '/^\+\+\+ / { next } /^\+/ { sub(/^\+/, ""); print }')

[ -z "$added" ] && exit 0

violation=$(printf '%s\n' "$added" | python3 -c "
import re, sys

for raw in sys.stdin.read().splitlines():
    # Strip double- and single-quoted string contents so quoted literals
    # (e.g. commit messages or grep patterns) do not count as a command.
    stripped = re.sub(r'\"[^\"]*\"', '\"\"', raw)
    stripped = re.sub(r\"'[^']*'\", \"''\", stripped)
    if re.search(r'(^|[|;&]|&&|\|\||\\\$\()\s*sqlite3\b', stripped):
        print(raw)
        break
" 2>/dev/null)

if [ -n "$violation" ]; then
  echo "ERROR: direct sqlite3 invocation detected in staged changes:" >&2
  echo "  $violation" >&2
  echo "" >&2
  echo "Use tusk CLI commands instead (tusk task-list / task-get / task-done / task-update)." >&2
  echo "If this is intentional, bypass with: git commit --no-verify" >&2
  exit 2
fi

exit 0
