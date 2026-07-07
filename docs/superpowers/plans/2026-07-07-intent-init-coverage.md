# Intent Init Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add regression coverage for the intent-driven `tusk-init` path without changing production behavior.

**Architecture:** Extend the existing focused pytest files for intent normalization, bootstrap planning, safe materialization, durable memory, and non-network init wizard flow. Keep tests hermetic by using pure helpers or temporary Tusk databases instead of GitHub/network calls.

**Tech Stack:** Python pytest, existing `bin/tusk` CLI, existing init helper modules.

---

### Task 1: Unit Coverage For Intent And Planning

**Files:**
- Modify: `tests/unit/test_init_intent.py`
- Modify: `tests/unit/test_init_bootstrap_plan.py`

- [ ] Add tests for alias-heavy interview answer normalization and archetype-driven module selection.
- [ ] Run targeted unit tests and confirm they pass.

### Task 2: Unit Coverage For Durable Memory Idempotency

**Files:**
- Modify: `tests/unit/test_init_apply_memory.py`

- [ ] Add a test showing repeated plan memory application skips context atoms, pillars, and glossary entries that already exist.
- [ ] Run targeted memory tests and confirm they pass.

### Task 3: Integration Coverage For Materialization And End-To-End Plan Generation

**Files:**
- Modify: `tests/integration/test_bootstrap_manifest_writer.py`
- Modify: `tests/integration/test_init_wizard.py`

- [ ] Add writer coverage for mixed safe operations and marker conflicts across reruns.
- [ ] Add a non-network `init-wizard` fixture that supplies bootstrap JSON directly and verifies plan generation, safe file materialization, memory seeding, and idempotent rerun behavior.
- [ ] Run targeted integration tests and confirm they pass.

### Task 4: Verify And Commit

**Files:**
- All modified files above.

- [ ] Run `python3 -m pytest tests/unit/test_init_intent.py tests/unit/test_init_bootstrap_plan.py tests/unit/test_init_apply_memory.py tests/integration/test_bootstrap_manifest_writer.py tests/integration/test_init_wizard.py -q`.
- [ ] Run `python3 -m pytest tests/unit/ -q`.
- [ ] Mark criteria 3550 through 3554 done and commit through Tusk.
