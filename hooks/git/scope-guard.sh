#!/bin/bash
# Git pre-commit guard: rejects commits whose staged paths fall outside the
# scope inferred for the current task (from task description / acceptance
# criteria / verification specs, via `task_referenced_paths`).
#
# Silent pass when:
#   - TUSK_NO_SCOPE_GUARD=1            (kill-switch)
#   - tusk is not on $PATH             (degrade gracefully)
#   - current branch does not parse to a task ID via `tusk branch-parse`
#   - the task has no scope signal (no referenced paths)
#
# Bypass when:
#   - TUSK_SCOPE_GUARD_BYPASS=1        (explicit override; logged to stderr)
#   - git is invoked with --no-verify  (e.g. `tusk commit ... --skip-verify`)
#
# Always-allowed paths come from `tusk config scope` -> always_allowed
# (defaults to VERSION, CHANGELOG.md, MANIFEST, .claude/tusk-manifest.json).
#
# Exit 2 on violation, 0 otherwise.

set -u

if [ "${TUSK_NO_SCOPE_GUARD:-0}" = "1" ]; then
  exit 0
fi

if [ "${TUSK_SCOPE_GUARD_BYPASS:-0}" = "1" ]; then
  echo "scope-guard: bypassed (TUSK_SCOPE_GUARD_BYPASS=1)" >&2
  exit 0
fi

if ! command -v tusk >/dev/null 2>&1; then
  exit 0
fi

# Parse task ID from the current branch. branch-parse exits 1 when the branch
# doesn't match feature/TASK-<id>-<slug> — silent pass in that case.
branch_json="$(tusk branch-parse 2>/dev/null)"
if [ -z "$branch_json" ]; then
  exit 0
fi
task_id="$(printf '%s' "$branch_json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("task_id", ""))
except Exception:
    pass
' 2>/dev/null)"
if [ -z "$task_id" ]; then
  exit 0
fi

# Pull the inferred scope. Empty output = no scope signal -> silent pass.
scope="$(tusk scope-paths "$task_id" 2>/dev/null)"
if [ -z "$scope" ]; then
  exit 0
fi

# Always-allowed list from config. Missing key -> empty.
allowed="$(tusk config scope 2>/dev/null | python3 -c '
import json, sys
try:
    obj = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for p in obj.get("always_allowed", []) or []:
    print(p)
' 2>/dev/null)"

# Staged files: include adds, copies, modifies, renames, deletes. Disable
# rename detection so renames show as a delete + add pair against the literal
# paths (matches how scope is described in task text).
staged="$(git diff --cached --name-only --no-renames --diff-filter=ACMRD 2>/dev/null)"
if [ -z "$staged" ]; then
  exit 0
fi

violations="$(SCOPE="$scope" ALLOWED="$allowed" STAGED="$staged" python3 -c '
import os, sys
scope = set(filter(None, (os.environ.get("SCOPE", "") or "").splitlines()))
allowed = set(filter(None, (os.environ.get("ALLOWED", "") or "").splitlines()))
staged = list(filter(None, (os.environ.get("STAGED", "") or "").splitlines()))
allow_set = scope | allowed
for f in staged:
    if f not in allow_set:
        print(f)
' 2>/dev/null)"

if [ -n "$violations" ]; then
  echo "ERROR: scope-guard rejected commit — staged paths outside task scope (TASK-$task_id):" >&2
  printf '%s\n' "$violations" | sed 's/^/  /' >&2
  echo "" >&2
  echo "Task scope (from description / acceptance criteria):" >&2
  printf '%s\n' "$scope" | sed 's/^/  /' >&2
  if [ -n "$allowed" ]; then
    echo "" >&2
    echo "Always-allowed paths (scope.always_allowed):" >&2
    printf '%s\n' "$allowed" | sed 's/^/  /' >&2
  fi
  echo "" >&2
  echo "If this is intentional, bypass with one of:" >&2
  echo "  tusk commit ... --skip-verify   (skips lint + pre-commit hooks)" >&2
  echo "  git commit --no-verify ...       (skips pre-commit hooks)" >&2
  echo "  TUSK_SCOPE_GUARD_BYPASS=1 ...   (override only this guard)" >&2
  exit 2
fi

exit 0
