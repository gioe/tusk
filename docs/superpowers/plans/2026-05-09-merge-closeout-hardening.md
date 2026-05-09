# Merge Closeout Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden `tusk merge` and `tusk abandon` closeout so task/session state is not left half-finalized when worktree cleanup or a transient missing project-local `tusk` wrapper interferes.

**Architecture:** Keep merge and abandon behavior local to the existing command modules. Add a small internal subprocess helper in `tusk-merge.py` and reuse it from `tusk-abandon.py`; reorder abandon cleanup so DB-affecting subprocess calls complete before recorded worktree removal.

**Tech Stack:** Python CLI modules, SQLite fixtures, pytest integration/unit-style command tests, git subprocess mocks.

---

### Task 1: Add Controlled Internal Tusk Invocation

**Files:**
- Modify: `bin/tusk-merge.py`
- Modify: `bin/tusk-abandon.py`
- Test: `tests/integration/test_merge_rebase_flag.py`
- Test: `tests/integration/test_abandon.py`

- [ ] **Step 1: Write failing merge test**

Add a test in `tests/integration/test_merge_rebase_flag.py` that monkeypatches `tusk_merge.run` to raise `FileNotFoundError` when the command invokes `session-close`, then asserts `tusk merge` returns nonzero with a targeted diagnostic instead of tracebacking.

- [ ] **Step 2: Run merge test red**

Run: `python3 -m pytest tests/integration/test_merge_rebase_flag.py::TestInternalTuskInvocation -q`

Expected: FAIL because `FileNotFoundError` escapes from `run([tusk_bin, ...])`.

- [ ] **Step 3: Implement helper**

In `bin/tusk-merge.py`, add `_run_tusk_subcommand(tusk_bin, args)` that calls `run([tusk_bin, *args], check=False)`, retries once only for `FileNotFoundError`, and returns a `CompletedProcess` with exit code 127 and a clear stderr diagnostic if the wrapper is still missing.

- [ ] **Step 4: Replace closeout subprocess calls**

Replace merge closeout calls to `run([tusk_bin, "session-close", ...])`, `run([tusk_bin, "task-done", ...])`, and `_autodetect_session` task-start calls where appropriate with `_run_tusk_subcommand`.

- [ ] **Step 5: Reuse helper in abandon**

Expose the helper via `tusk_abandon._run_tusk_subcommand = _merge._run_tusk_subcommand` and use it for abandon `session-close`, `task-done`, and autodetect calls.

- [ ] **Step 6: Run targeted tests green**

Run: `python3 -m pytest tests/integration/test_merge_rebase_flag.py::TestInternalTuskInvocation tests/integration/test_abandon.py -q`

Expected: PASS.

### Task 2: Reorder Abandon Recorded Worktree Cleanup

**Files:**
- Modify: `bin/tusk-abandon.py`
- Test: `tests/integration/test_abandon.py`

- [ ] **Step 1: Write failing ordering test**

Add a test in `tests/integration/test_abandon.py` for a recorded task workspace. Mock subprocess calls and assert the observed order is `session-close`, `task-done`, then `git worktree remove`.

- [ ] **Step 2: Run ordering test red**

Run: `python3 -m pytest tests/integration/test_abandon.py::TestAbandonRecordedWorktreeCleanup -q`

Expected: FAIL because the current implementation removes the recorded worktree before closing session/task.

- [ ] **Step 3: Move cleanup later**

In `bin/tusk-abandon.py`, keep the existing branch safety checks before closeout, but defer `_remove_recorded_task_worktree()` and `git branch -D` until after successful `session-close` and `task-done`.

- [ ] **Step 4: Preserve safety behavior**

Ensure dirty worktree cleanup failures still return nonzero after task closeout with actionable retry guidance, and unmerged commits still block before any DB state changes.

- [ ] **Step 5: Run targeted abandon tests**

Run: `python3 -m pytest tests/integration/test_abandon.py -q`

Expected: PASS.

### Task 3: Verify Merge Cluster Coverage

**Files:**
- Modify only if tests reveal a regression.

- [ ] **Step 1: Run merge and summary suite**

Run: `python3 -m pytest tests/integration/test_merge_rebase_flag.py tests/integration/test_merge_linked_worktree_default_locked.py tests/integration/test_abandon.py tests/unit/test_task_summary.py -q`

Expected: PASS.

- [ ] **Step 2: Run repository status check**

Run: `git status --short`

Expected: Only intentional plan/code/test changes.

- [ ] **Step 3: Prepare issue summary**

Report which merge-cluster issues are fixed by this patch, which were already fixed in current source, and any residual risk for install/upgrade atomicity.
