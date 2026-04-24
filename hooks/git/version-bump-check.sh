#!/bin/bash
# Git pre-push guard: blocks pushes that change distributable files
# (bin/*, skills/*, config.default.json, install.sh) without bumping VERSION.
#
# Exits 0 when the guard cannot determine the remote default branch (no
# origin configured, branch not tracked) — the check is a no-op rather
# than a false positive.
#
# Exit 2 on violation, 0 otherwise. Bypass with `git push --no-verify`.

default_branch=$(git remote show origin 2>/dev/null | awk '/HEAD branch/ {print $NF}')
[ -z "$default_branch" ] && exit 0

# Can't compare against origin/<default_branch> if the remote ref isn't present
if ! git rev-parse --verify --quiet "origin/${default_branch}" >/dev/null; then
  exit 0
fi

changed=$(git diff --name-only "origin/${default_branch}..HEAD" 2>/dev/null)
[ -z "$changed" ] && exit 0

dist_changed=0
while IFS= read -r f; do
  case "$f" in
    bin/*|skills/*|config.default.json|install.sh)
      dist_changed=1
      break
      ;;
  esac
done <<< "$changed"

[ "$dist_changed" -eq 0 ] && exit 0

if echo "$changed" | grep -qx 'VERSION'; then
  exit 0
fi

echo "ERROR: distributable files (bin/, skills/, config.default.json, or install.sh) changed but VERSION was not bumped." >&2
echo "Bump VERSION and update CHANGELOG before pushing." >&2
echo "If this is intentional, bypass with: git push --no-verify" >&2
exit 2
