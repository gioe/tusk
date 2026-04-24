#!/bin/bash
# Git pre-commit guard: blocks commits that introduce `!=` in SQL contexts.
# Shell history expansion breaks `!=` — use `<>` instead.
#
# Scans the staged diff for *added* lines in .sh files that contain `!=`
# alongside a SQL keyword (WHERE, AND, OR, HAVING, CHECK, WHEN, SET). This
# mirrors tusk-lint Rule 2 but runs at git-event time so the violation is
# caught before the commit lands.
#
# Exit 2 on violation, 0 otherwise. Bypass with `git commit --no-verify`.

set -u

added=$(git diff --cached --unified=0 --no-color --diff-filter=ACMR -- '*.sh' 2>/dev/null | \
  awk '/^\+\+\+ / { next } /^\+/ { sub(/^\+/, ""); print }')

[ -z "$added" ] && exit 0

violation=$(printf '%s\n' "$added" | python3 -c "
import sys

KEYWORDS = ('WHERE', 'AND ', 'OR ', 'HAVING', 'CHECK', 'WHEN ', 'SET ')
for raw in sys.stdin.read().splitlines():
    if '!=' not in raw:
        continue
    upper = raw.upper()
    if any(kw in upper for kw in KEYWORDS):
        print(raw)
        break
" 2>/dev/null)

if [ -n "$violation" ]; then
  echo "ERROR: SQL '!=' operator detected in staged changes:" >&2
  echo "  $violation" >&2
  echo "" >&2
  echo "Use <> instead of != in SQL — shell history expansion breaks !=." >&2
  echo "If this is intentional, bypass with: git commit --no-verify" >&2
  exit 2
fi

exit 0
