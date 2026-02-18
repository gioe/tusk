#!/bin/bash
# PostToolUse hook: runs tusk lint when skills/ or bin/ files are edited.
# Non-blocking — always exits 0. Violations surface as additionalContext.

input=$(cat)

# Extract the file path from tool_input
file_path=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null)

[ -z "$file_path" ] && exit 0

# Resolve repo root for relative-path matching
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

# Strip repo root to get the relative path
rel_path="${file_path#"$repo_root"/}"

# Only lint when the file is under skills/ or bin/
case "$rel_path" in
  skills/*|bin/*) ;;
  *) exit 0 ;;
esac

# Resolve tusk binary — don't rely on PATH (SessionStart hook may not have run yet)
if command -v tusk &>/dev/null; then
  TUSK=tusk
elif [ -x "$repo_root/.claude/bin/tusk" ]; then
  TUSK="$repo_root/.claude/bin/tusk"
elif [ -x "$repo_root/bin/tusk" ]; then
  TUSK="$repo_root/bin/tusk"
else
  exit 0
fi

# Run tusk lint and capture output; use exit code to detect violations
lint_output=$("$TUSK" lint 2>&1)
lint_rc=$?

# Exit code 0 = no violations, nothing to report
[ "$lint_rc" -eq 0 ] && exit 0

# Return violations as additionalContext
python3 -c "
import json, sys
ctx = sys.argv[1]
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': 'tusk lint found convention violations after editing ' + sys.argv[2] + ':\n' + ctx
    }
}))
" "$lint_output" "$rel_path"

exit 0
