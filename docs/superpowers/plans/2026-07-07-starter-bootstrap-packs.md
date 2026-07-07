# Starter Bootstrap Packs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add schema-validated starter bootstrap-pack examples and placeholder contracts for current and future utility repos.

**Architecture:** Store examples under `docs/bootstrap-packs/` so this repo owns the contract without writing to external repos. Validate JSON examples with the same `tusk-init-fetch-bootstrap.py` validator used for fetched utility manifests, and update docs to point maintainers at the examples.

**Tech Stack:** JSON, Markdown, Python pytest unit tests, existing Tusk bootstrap validator.

---

### Task 1: Add Failing Example-Contract Tests

**Files:**
- Create: `tests/unit/test_bootstrap_pack_examples.py`

- [ ] **Step 1: Write tests that require example pack files**

Create tests that load `docs/bootstrap-packs/ios-libs/tusk-bootstrap.json`, validate it with `bin/tusk-init-fetch-bootstrap.py::_validate`, and assert it includes modules for SharedKit, APIClient, navigation, persistence, observability, and tests. Add tests that require placeholder contract docs for `android-libs`, `web-libs`, and `backend-libs`, and require selector behavior to skip unavailable optional packs.

- [ ] **Step 2: Run tests and confirm red**

Run: `python3 -m pytest tests/unit/test_bootstrap_pack_examples.py -q`

Expected: failures report missing `docs/bootstrap-packs/...` files.

### Task 2: Add Bootstrap Pack Examples

**Files:**
- Create: `docs/bootstrap-packs/README.md`
- Create: `docs/bootstrap-packs/ios-libs/tusk-bootstrap.json`
- Create: `docs/bootstrap-packs/android-libs/CONTRACT.md`
- Create: `docs/bootstrap-packs/web-libs/CONTRACT.md`
- Create: `docs/bootstrap-packs/backend-libs/CONTRACT.md`

- [ ] **Step 1: Add rich ios-libs manifest**

Create a valid schema-v2 manifest with modules, safe file specs, tasks, context atoms, pillars, glossary entries, dependencies, and verification hints.

- [ ] **Step 2: Add placeholder contracts**

Document the expected pack shape and starter modules for Android, web, and backend utility repos.

- [ ] **Step 3: Run tests and confirm green**

Run: `python3 -m pytest tests/unit/test_bootstrap_pack_examples.py -q`

Expected: all tests pass.

### Task 3: Wire Documentation References

**Files:**
- Modify: `docs/DOMAIN.md`
- Modify: `README.md`
- Modify: `skills/tusk-init/SKILL.md`
- Modify: `codex-prompts/tusk-init.md`

- [ ] **Step 1: Point maintainers at examples**

Add concise links from the bootstrap-pack schema docs and init docs to `docs/bootstrap-packs/`.

- [ ] **Step 2: Run targeted tests**

Run: `python3 -m pytest tests/unit/test_bootstrap_pack_examples.py tests/unit/test_init_bootstrap_select.py -q`

Expected: all tests pass.

### Task 4: Verify And Commit

**Files:**
- All modified files above.

- [ ] **Step 1: Run the unit suite**

Run: `python3 -m pytest tests/unit/ -q`

Expected: all tests pass.

- [ ] **Step 2: Complete criteria and commit through Tusk**

Mark criteria 3546 through 3549 done, then commit the task with `TUSK_DB=/Users/mattgioe/Desktop/projects/tusk/tusk/tasks.db ./bin/tusk commit 762 "Create starter bootstrap pack examples" ... --criteria 3546 --criteria 3547 --criteria 3548 --criteria 3549`.
