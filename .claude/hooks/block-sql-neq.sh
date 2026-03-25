#!/bin/bash
# PreToolUse hook: blocks != in SQL contexts.
# Shell history expansion breaks != — use <> instead.
#
# Uses a subcommand whitelist: only tusk subcommands that execute raw SQL
# (shell, sql-query) are checked. All other subcommands are unconditionally
# allowed, regardless of quoting — this eliminates false positives and false
# negatives without any quote-stripping tradeoff.

SQL_SUBCOMMANDS="shell sql-query"

# Read JSON from stdin
input=$(cat)

# Extract the tusk subcommand from the command string.
# Returns the first argument after the (bin/)tusk invocation.
subcommand=$(echo "$input" | python3 -c "
import sys, json, re
data = json.load(sys.stdin)
cmd = data.get('tool_input', {}).get('command', '')
m = re.search(r'(?:^|[|;&]|&&|\|\||\\\$\()\s*(?:bin/)?tusk\s+(\S+)', cmd)
print(m.group(1) if m else '')
" 2>/dev/null)

# Not a tusk invocation — nothing to check
[ -z "$subcommand" ] && exit 0

# Only check SQL-executing subcommands; all others are unconditionally allowed
is_sql_subcommand=0
for sc in $SQL_SUBCOMMANDS; do
    if [ "$subcommand" = "$sc" ]; then
        is_sql_subcommand=1
        break
    fi
done

[ "$is_sql_subcommand" -eq 0 ] && exit 0

# Extract the full command to check for !=
command=$(echo "$input" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('command', ''))
" 2>/dev/null)

# Block != in SQL-executing subcommand invocations
if echo "$command" | grep -q '!='; then
    echo "Use <> instead of != in SQL — shell history expansion breaks !=."
    exit 2
fi

exit 0
