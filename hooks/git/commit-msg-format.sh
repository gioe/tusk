#!/bin/bash
# Git commit-msg guard: enforces the [TASK-<id>] prefix on commits made from
# a feature/TASK-<id>-<slug> branch.
#
# $1 is the path to the commit message file (git convention). The hook reads
# the first non-comment, non-empty line and requires a [TASK-<id>] prefix.
#
# Exit 2 on violation, 0 otherwise. Bypass with `git commit --no-verify`.

msg_file="${1:-}"
if [ -z "$msg_file" ] || [ ! -f "$msg_file" ]; then
  exit 0
fi

branch=$(git branch --show-current 2>/dev/null)

# Only enforce on feature/TASK-<id>-* branches — other branches skip the check
if [[ ! "$branch" =~ ^feature/TASK-[0-9]+-. ]]; then
  exit 0
fi

# Extract expected task ID from the branch name
expected_id="${branch#feature/TASK-}"
expected_id="${expected_id%%-*}"

# Read the first non-comment, non-empty line of the commit message
first_line=""
while IFS= read -r line; do
  case "$line" in
    '#'*) continue ;;
    '')   continue ;;
    *)    first_line="$line"; break ;;
  esac
done < "$msg_file"

# Empty message (git will reject later) — nothing to check
[ -z "$first_line" ] && exit 0

# Allow merge/revert commits (git generates these messages)
case "$first_line" in
  "Merge "*|"Revert "*) exit 0 ;;
esac

# Commit message must start with [TASK-<id>]
if [[ "$first_line" =~ ^\[TASK-[0-9]+\] ]]; then
  exit 0
fi

echo "Warning: commit message does not start with [TASK-<id>]. Use 'tusk commit <id> \"<message>\" <files>' to enforce this format automatically." >&2
echo "Expected prefix: [TASK-$expected_id]" >&2
echo "If this is intentional, bypass with: git commit --no-verify" >&2
exit 2
