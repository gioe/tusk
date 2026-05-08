# Task Worktree Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add normal task-owned worktree creation, reuse, and listing commands independent of bakeoff shadow rows.

**Architecture:** Add a small `task_workspaces` table to persist task workspace metadata, a focused `bin/tusk-task-worktree.py` command module for create/list behavior, and dispatcher/schema/docs updates. Reuse the existing git worktree patterns from `tusk-bakeoff.py`, but keep bakeoff state and task workspace state separate.

**Tech Stack:** Bash dispatcher, Python CLI modules, SQLite, pytest integration tests, git worktree porcelain output.

---

### Task 1: Schema And Dispatcher

**Files:**
- Modify: `bin/tusk`
- Modify: `bin/tusk-migrate.py`
- Modify: `docs/DOMAIN.md`
- Test: `tests/integration/test_task_worktree.py`

- [ ] **Step 1: Write the failing schema/dispatch test**

Add an integration test that runs `tusk task-worktree list --format json` against a fresh temp install and expects `[]`, proving the dispatcher exists and the fresh schema includes `task_workspaces`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/test_task_worktree.py::TestTaskWorktreeList::test_empty_list_defaults_to_json_array -q`
Expected: FAIL because `task-worktree` is not dispatched.

- [ ] **Step 3: Add schema and dispatcher**

Add `task_workspaces` to `cmd_init`, add migration 68 for existing DBs, register `task-worktree` in the dispatcher, and create the minimal command module that returns an empty JSON list.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/test_task_worktree.py::TestTaskWorktreeList::test_empty_list_defaults_to_json_array -q`
Expected: PASS.

### Task 2: Create And Reuse

**Files:**
- Modify: `bin/tusk-task-worktree.py`
- Test: `tests/integration/test_task_worktree.py`

- [ ] **Step 1: Write failing create/reuse tests**

Add tests that seed a task, call `tusk task-worktree create <id> <slug> --workspace-root <tmp>`, assert JSON fields include `task_id`, `branch`, `workspace_path`, `workspace_id`, and `created=true`, then call again and assert `created=false` with the same path and id.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/integration/test_task_worktree.py::TestTaskWorktreeCreate -q`
Expected: FAIL because create is not implemented.

- [ ] **Step 3: Implement create/reuse**

Implement branch naming as `feature/TASK-<id>-<slug>`, default base branch detection, `git worktree add -b`, task lookup, branch collision detection, and idempotent reuse of the active recorded workspace.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/integration/test_task_worktree.py::TestTaskWorktreeCreate -q`
Expected: PASS.

### Task 3: Reconciliation Listing

**Files:**
- Modify: `bin/tusk-task-worktree.py`
- Test: `tests/integration/test_task_worktree.py`

- [ ] **Step 1: Write failing reconciliation tests**

Add tests that remove a created worktree outside tusk, run `tusk task-worktree list --format json`, and assert the row remains recorded with `exists_on_disk=false` and no matching live worktree path.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/integration/test_task_worktree.py::TestTaskWorktreeList -q`
Expected: FAIL until reconciliation status is implemented.

- [ ] **Step 3: Implement list/status reconciliation**

Parse `git worktree list --porcelain`, join recorded rows with live branch/path data, and emit compact JSON. Keep the command read-only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/integration/test_task_worktree.py -q`
Expected: PASS.

### Task 4: Release Metadata And Verification

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update docs and release metadata**

Document `task-worktree` in the command list, bump `VERSION` once, and add a dated `CHANGELOG.md` entry for the distribution change.

- [ ] **Step 2: Run focused and full verification**

Run: `python3 -m pytest tests/integration/test_task_worktree.py -q`
Run: `python3 -m pytest tests/ -q`
Expected: focused tests pass; full suite passes or unrelated failures are captured with evidence.

- [ ] **Step 3: Commit**

Run: `bin/tusk commit 351 "Add task worktree commands" <changed files> --criteria 1620 --criteria 1621 --criteria 1622 --criteria 1623`
Expected: commit succeeds and criteria are linked to the commit.

