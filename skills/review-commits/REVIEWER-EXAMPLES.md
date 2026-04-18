# Reviewer Prompt Examples

Illustrative examples for rules stated in `REVIEWER-PROMPT.md`. Not passed to the agent at runtime — kept here so human maintainers can see the reasoning behind the rule without bloating the prompt sent on every review run.

## Wrappers and Delegation Layers — Rule from Step 2

Inability to fully trace a subtree or call chain is *not* sufficient evidence to flag a wrapper as unused at `must_fix`. When in doubt, use `defer`.

**Example (React):** A reviewer traces `LoginModal → FullScreenModal → LaughtrackLogin` and stops, concluding `StyleContextProvider` has no `useStyleContext` consumer. The actual consumer lives at `LoginModal → LaughtrackLogin → LoginForm → FormInput → EmailInput → Input → useStyleContext()`. Stopping early produces a false positive that reverts a correct fix.

**Example (Python middleware):** A reviewer sees `AuthMiddleware` wrapping a view and finds no direct calls to `request.user` in the top-level handler, concluding the middleware is unused. The actual usage is in a utility called three frames deeper: `handler → process_request → validate_permissions → request.user`. Same mistake, different stack.

## Final-State Verification — Rule from Step 2.5

Before flagging `must_fix`, run `git show HEAD:<file> | grep -n "<pattern>"` to confirm the pattern is present in the final state. If absent, check whether the code was moved before discarding.

**Example (single-file removal):** The diff shows a `-` line removing `ORDER BY RANDOM()` and a `+` line adding `ORDER BY show_count DESC`. `git show HEAD:path/to/file.py | grep "RANDOM()"` returns no output, and `git diff | grep "^+" | grep "RANDOM()"` also returns nothing — the pattern is gone from the codebase. False positive; do not flag.

**Example (moved code):** The diff removes `def validate_user(...)` from `auth/utils.py` and adds it to `auth/validators.py`. `git show HEAD:auth/utils.py | grep "validate_user"` returns absent. The cross-file search `git diff ... | grep "^+" | grep "validate_user"` returns hits under `+++ b/auth/validators.py`. Confirm the pattern in `auth/validators.py` and update the finding to reference that file rather than discarding.

## Record Your Findings — Example from Step 3

```bash
tusk review add-comment {review_id} "SQL query uses string interpolation — SQL injection risk" \
  --file "bin/tusk-example.py" \
  --line-start 42 \
  --category must_fix \
  --severity critical
```
