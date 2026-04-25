#!/bin/bash
# PostToolUse hook: runs tusk lint when skills/ or bin/ files are edited.
# Non-blocking — always exits 0. Violations surface as additionalContext.

source "$(dirname "$0")/hook-common.sh"

input=$(cat)

# Extract the file path from tool_input
file_path=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null)

[ -z "$file_path" ] && exit 0

# Strip repo root to get the relative path
rel_path="${file_path#"$REPO_ROOT"/}"

# Only lint when the file is under skills/ or bin/
case "$rel_path" in
  skills/*|bin/*) ;;
  *) exit 0 ;;
esac

# Run tusk lint and capture output; use exit code to detect violations
lint_output=$("$TUSK" lint 2>&1)
lint_rc=$?

# Exit code 0 = no violations, nothing to report
[ "$lint_rc" -eq 0 ] && exit 0

# Resolve provenance: the directory packaging tusk-lint.py (and its VERSION)
# sits next to the resolved tusk binary. When $TUSK fell through to PATH,
# resolve the absolute path so the stamp names the install that actually ran.
case "$TUSK" in
  /*) tusk_real="$TUSK" ;;
  *)  tusk_real=$(command -v "$TUSK" 2>/dev/null) ;;
esac
tusk_dir=$(dirname "$tusk_real" 2>/dev/null)
tusk_version=$(cat "$tusk_dir/VERSION" 2>/dev/null || echo unknown)

# Return violations as additionalContext
python3 -c "
import json, sys
ctx = sys.argv[1]
path = sys.argv[2]
tusk_path = sys.argv[3]
version = sys.argv[4]
header = 'tusk lint found convention violations after editing ' + path + ' (via ' + tusk_path + ', lint VERSION ' + version + '):'
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': header + '\n' + ctx
    }
}))
" "$lint_output" "$rel_path" "${tusk_real:-$TUSK}" "$tusk_version"

exit 0
