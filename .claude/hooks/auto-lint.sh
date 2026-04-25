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

# Resolve tusk binary — prefer in-repo paths so the hook always runs the
# same lint version as the source tree it's editing. A PATH-resolved tusk
# can point at another project's installed copy (e.g. via ~/.local/bin
# wrappers), and a stale rule18 there reports phantom MANIFEST 'extra'
# entries against the current MANIFEST. Source repo: bin/tusk first.
# Target project: .claude/bin/tusk first. PATH is the last resort.
if [ -x "$repo_root/bin/tusk" ]; then
  TUSK="$repo_root/bin/tusk"
elif [ -x "$repo_root/.claude/bin/tusk" ]; then
  TUSK="$repo_root/.claude/bin/tusk"
elif command -v tusk &>/dev/null; then
  TUSK=tusk
else
  exit 0
fi

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
