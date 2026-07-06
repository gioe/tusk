# Tusk Init Vertical-Slice Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and optionally seed first vertical-slice backlog tasks from `tusk-init` intent.

**Architecture:** Add a pure `bin/tusk-init-vertical-slice.py` generator, compose its output into `bin/tusk-init-bootstrap-plan.py`, and let `bin/tusk-init-wizard.py` seed accepted plan tasks. The bootstrap plan remains the single reviewable contract and all side effects stay behind explicit materialization gates.

**Tech Stack:** Python 3 CLI scripts, repo-local `bin/tusk` dispatcher, SQLite-backed task insertion, pytest unit and integration tests.

---

### Task 1: Pure Vertical-Slice Generator

**Files:**
- Create: `bin/tusk-init-vertical-slice.py`
- Test: `tests/unit/test_init_vertical_slice.py`

- [ ] **Step 1: Write failing mobile/web/backend generator tests**

Create `tests/unit/test_init_vertical_slice.py` with direct module loading and these tests:

```python
from __future__ import annotations

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-init-vertical-slice.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_init_vertical_slice", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _criterion_text(task):
    return " ".join(task["criteria"]).lower()


def test_mobile_proposal_uses_workflow_data_verification_and_docs():
    mod = _load_module()

    tasks = mod.generate_vertical_slice_tasks(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "primary_workflows": ["capture inspection"],
                "platforms": ["ios"],
                "data_needs": ["inspections"],
                "integrations": ["weather api"],
                "quality_priorities": ["offline support"],
            },
        },
        archetype={"id": "consumer_ios_app"},
        selected_modules=[{"id": "sharedkit", "name": "SharedKit"}],
    )

    assert len(tasks) == 1
    task = tasks[0]
    assert task["id"] == "vertical-slice-mobile-capture-inspection"
    assert "capture inspection" in task["summary"].lower()
    criteria = _criterion_text(task)
    assert "screen" in criteria or "ui" in criteria
    assert "inspections" in criteria
    assert "weather api" in criteria
    assert "test" in criteria
    assert "document" in criteria


def test_web_proposal_uses_route_state_integration_verification_and_docs():
    mod = _load_module()

    tasks = mod.generate_vertical_slice_tasks(
        picked={
            "project_type": "web_app",
            "init_intent": {
                "primary_workflows": ["review customer queue"],
                "platforms": ["web"],
                "data_needs": ["customers"],
                "integrations": ["stripe"],
                "quality_priorities": ["keyboard workflows"],
            },
        },
        archetype={"id": "internal_dashboard"},
        selected_modules=[],
    )

    task = tasks[0]
    assert task["id"] == "vertical-slice-web-review-customer-queue"
    criteria = _criterion_text(task)
    assert "route" in criteria or "page" in criteria
    assert "customers" in criteria
    assert "stripe" in criteria
    assert "test" in criteria
    assert "document" in criteria


def test_backend_proposal_uses_endpoint_schema_integration_verification_and_docs():
    mod = _load_module()

    tasks = mod.generate_vertical_slice_tasks(
        picked={
            "project_type": "python_service",
            "init_intent": {
                "primary_workflows": ["submit intake request"],
                "platforms": ["api"],
                "data_needs": ["intake requests"],
                "integrations": ["postgres"],
                "quality_priorities": ["audit trail"],
            },
        },
        archetype={"id": "api_service"},
        selected_modules=[{"id": "structured-logging", "name": "Structured logging"}],
    )

    task = tasks[0]
    assert task["id"] == "vertical-slice-backend-submit-intake-request"
    criteria = _criterion_text(task)
    assert "endpoint" in criteria or "service" in criteria
    assert "intake requests" in criteria
    assert "postgres" in criteria
    assert "test" in criteria
    assert "document" in criteria
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/unit/test_init_vertical_slice.py -q`

Expected: fail because `bin/tusk-init-vertical-slice.py` does not exist.

- [ ] **Step 3: Implement minimal generator**

Create `bin/tusk-init-vertical-slice.py` with:

```python
#!/usr/bin/env python3
"""Generate deterministic first vertical-slice task proposals for tusk-init."""

from __future__ import annotations

import re
from typing import Any


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw = value
    else:
        raw = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text or "first-product-workflow"


def _first(items: list[str], fallback: str) -> str:
    return items[0] if items else fallback


def _family(project_type: str, archetype_id: str, platforms: list[str]) -> str:
    haystack = " ".join([project_type, archetype_id, *platforms]).lower()
    if any(token in haystack for token in ("ios", "android", "mobile")):
        return "mobile"
    if any(token in haystack for token in ("api", "backend", "service", "python")):
        return "backend"
    return "web"


def generate_vertical_slice_tasks(
    *,
    picked: dict[str, Any],
    archetype: dict[str, Any] | None = None,
    selected_modules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    intent = picked.get("init_intent") or {}
    archetype = archetype or {}
    workflows = _list(intent.get("primary_workflows"))
    platforms = _list(intent.get("platforms"))
    entities = _list(intent.get("data_needs"))
    integrations = _list(intent.get("integrations"))
    qualities = _list(intent.get("quality_priorities"))
    modules = [m.get("name") or m.get("id") for m in selected_modules or [] if m.get("name") or m.get("id")]

    workflow = _first(workflows, "first product workflow")
    entity = _first(entities, "core project entity")
    integration = _first(integrations, "selected integration boundary")
    quality = _first(qualities, "project quality goal")
    module_text = ", ".join(modules) if modules else "selected starter modules"
    project_type = str(picked.get("project_type") or intent.get("project_type") or "")
    family = _family(project_type, str(archetype.get("id") or ""), platforms)

    if family == "mobile":
        summary = f"Build first mobile slice: {workflow}"
        criteria = [
            f"The app exposes the {workflow} screen flow with the selected UI conventions.",
            f"The slice creates, reads, or updates {entity} data with a clear empty/loading/error state.",
            f"The slice wires {integration} and {module_text} only where needed for the workflow.",
            f"Automated tests or a documented simulator check verify the {workflow} behavior.",
            f"Project docs record how the {workflow} slice is structured and verified.",
        ]
    elif family == "backend":
        summary = f"Build first API slice: {workflow}"
        criteria = [
            f"The service exposes an endpoint, job, or command for {workflow}.",
            f"The slice validates and persists {entity} data or documents the temporary storage boundary.",
            f"The slice isolates {integration} behind a small integration boundary.",
            f"API or service tests verify success and failure behavior for {workflow}.",
            f"Project docs record the endpoint contract and verification command.",
        ]
    else:
        summary = f"Build first web slice: {workflow}"
        criteria = [
            f"The app exposes a route or page for {workflow} with useful empty/loading/error states.",
            f"The slice reads or updates {entity} data through the chosen state/data boundary.",
            f"The slice wires {integration} and {module_text} only where needed for the workflow.",
            f"Automated tests or a documented browser check verify the {workflow} behavior.",
            f"Project docs record the route, data contract, and verification command.",
        ]

    return [{
        "id": f"vertical-slice-{family}-{_slug(workflow)}",
        "summary": summary,
        "description": (
            f"Create the first shippable vertical slice for {workflow}. "
            f"Connect behavior, {entity} data, integrations, tests, and documentation so a future agent can build from the init plan."
        ),
        "priority": "High",
        "task_type": "feature",
        "complexity": "M",
        "criteria": criteria,
        "source": f"vertical_slice:{archetype.get('id') or project_type or family}",
    }]
```

- [ ] **Step 4: Run generator tests and verify GREEN**

Run: `python3 -m pytest tests/unit/test_init_vertical_slice.py -q`

Expected: all tests pass.

### Task 2: Compose Generated Tasks Into Bootstrap Plans

**Files:**
- Modify: `bin/tusk-init-bootstrap-plan.py`
- Test: `tests/unit/test_init_bootstrap_plan.py`

- [ ] **Step 1: Write failing plan-composition and task-control tests**

Add tests to `tests/unit/test_init_bootstrap_plan.py`:

```python
def test_plan_includes_vertical_slice_tasks_from_intent():
    mod = _load_module()

    plan = mod.build_bootstrap_plan(
        picked={
            "project_type": "ios_app",
            "init_intent": {
                "primary_workflows": ["capture inspection"],
                "platforms": ["ios"],
                "data_needs": ["inspections"],
                "integrations": ["weather api"],
                "quality_priorities": ["offline support"],
            },
        },
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
    )

    task = next(t for t in plan["tasks_to_create"] if t["source"].startswith("vertical_slice:"))
    assert task["id"] == "vertical-slice-mobile-capture-inspection"
    assert "capture inspection" in task["summary"].lower()
    assert any("test" in c.lower() for c in task["criteria"])


def test_plan_task_controls_can_pick_remove_skip_and_add_tasks():
    mod = _load_module()
    manual = {
        "id": "vertical-slice-manual",
        "summary": "Build edited starter slice",
        "description": "Edited by operator.",
        "priority": "High",
        "task_type": "feature",
        "complexity": "S",
        "criteria": ["Edited behavior is verified."],
    }

    picked_plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
        task_mode="pick",
        task_ids=["vertical-slice-mobile-capture-inspection"],
    )
    assert [t["id"] for t in picked_plan["tasks_to_create"]] == ["vertical-slice-mobile-capture-inspection"]

    removed_plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
        remove_tasks=["vertical-slice-mobile-capture-inspection"],
        add_tasks=[manual],
    )
    assert [t["id"] for t in removed_plan["tasks_to_create"]] == ["vertical-slice-manual"]

    skipped_plan = mod.build_bootstrap_plan(
        picked={"project_type": "ios_app", "init_intent": {"primary_workflows": ["capture inspection"], "platforms": ["ios"]}},
        archetype={"id": "consumer_ios_app"},
        bootstrap={"libs": []},
        task_mode="none",
    )
    assert skipped_plan["tasks_to_create"] == []
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/unit/test_init_bootstrap_plan.py -q`

Expected: fail because `task_mode`, `task_ids`, `remove_tasks`, `add_tasks`, and vertical-slice composition are missing.

- [ ] **Step 3: Implement plan composition and controls**

In `bin/tusk-init-bootstrap-plan.py`:

- Load `tusk-init-vertical-slice.py` via `_load_script_module`.
- Extend `build_bootstrap_plan(...)` parameters with `task_mode="all"`, `task_ids=None`, `remove_tasks=None`, and `add_tasks=None`.
- After module selection, append generated vertical-slice tasks.
- Apply controls in this order: `task_mode`, `remove_tasks`, `add_tasks`.
- Raise `ValueError` on unknown picked/removed task IDs.
- Add CLI flags:
  - `--task-mode`, `--plan-task-mode`, choices `all|none|pick`
  - `--task-id`, `--plan-task-id`, repeatable
  - `--remove-task`, `--plan-remove-task`, repeatable
  - `--add-task`, `--plan-add-task`, repeatable JSON object

Use helper functions shaped like:

```python
def _task_id(task: dict[str, Any]) -> str:
    return str(task.get("id") or task.get("summary") or "").strip()


def _apply_task_controls(tasks, *, task_mode, task_ids, remove_tasks, add_tasks):
    known = {_task_id(task) for task in tasks if _task_id(task)}
    pick_set = set(_list(task_ids))
    remove_set = set(_list(remove_tasks))
    unknown = (pick_set | remove_set) - known
    if unknown:
        raise ValueError(f"unknown plan task id(s): {', '.join(sorted(unknown))}")
    if task_mode == "none":
        tasks = []
    elif task_mode == "pick":
        tasks = [task for task in tasks if _task_id(task) in pick_set]
    tasks = [task for task in tasks if _task_id(task) not in remove_set]
    tasks.extend(add_tasks or [])
    return tasks
```

- [ ] **Step 4: Run plan tests and verify GREEN**

Run: `python3 -m pytest tests/unit/test_init_bootstrap_plan.py tests/unit/test_init_vertical_slice.py -q`

Expected: all tests pass.

### Task 3: Seed Accepted Plan Tasks In Init Wizard

**Files:**
- Modify: `bin/tusk-init-wizard.py`
- Test: `tests/integration/test_init_wizard.py`

- [ ] **Step 1: Write failing wizard materialization tests**

Add tests to `tests/integration/test_init_wizard.py`:

```python
def test_seed_plan_tasks_materializes_vertical_slice_task(codex_like_project):
    intent = {
        "primary_workflows": ["submit intake request"],
        "platforms": ["api"],
        "data_needs": ["intake requests"],
        "integrations": ["postgres"],
        "quality_priorities": ["audit trail"],
        "project_type": "python_service",
    }

    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--project-type", "python_service",
        "--init-intent", json.dumps(intent),
        "--plan-action", "accept",
        "--seed-plan-tasks", "all",
    )

    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["seeded_tasks"]
    assert payload["seeded_tasks"][0]["summary"] == "Build first API slice: submit intake request"

    listed = _run_tusk(codex_like_project, "task-list", "--format", "json", "--all")
    assert listed.returncode == 0, listed.stderr
    tasks = json.loads(listed.stdout)
    assert any(t["summary"] == "Build first API slice: submit intake request" for t in tasks)


def test_seed_plan_tasks_skip_materialization_writes_no_tasks(codex_like_project):
    intent = {
        "primary_workflows": ["submit intake request"],
        "platforms": ["api"],
        "project_type": "python_service",
    }

    result = _run(
        codex_like_project,
        "--non-interactive",
        "--no-auto-scan",
        "--project-type", "python_service",
        "--init-intent", json.dumps(intent),
        "--plan-action", "skip-materialization",
        "--seed-plan-tasks", "all",
    )

    assert result.returncode == 0, f"wizard failed:\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["seeded_tasks"] == []

    listed = _run_tusk(codex_like_project, "task-list", "--format", "json", "--all")
    assert listed.returncode == 0, listed.stderr
    tasks = json.loads(listed.stdout)
    assert not any(t["summary"] == "Build first API slice: submit intake request" for t in tasks)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/integration/test_init_wizard.py::test_seed_plan_tasks_materializes_vertical_slice_task tests/integration/test_init_wizard.py::test_seed_plan_tasks_skip_materialization_writes_no_tasks -q`

Expected: fail because `--seed-plan-tasks` is not recognized and wizard does not seed plan tasks.

- [ ] **Step 3: Implement wizard flags, forwarding, and plan task insertion**

In `bin/tusk-init-wizard.py`:

- Add parser flags for `--seed-plan-tasks all|none`, task mode, task IDs, remove task IDs, and add task JSON.
- Include `seed_plan_tasks == "all"` in `materialization_requested`.
- Pass task controls through `_build_plan(...)` into `build_bootstrap_plan(...)`.
- Add `_seed_tasks_from_plan(plan, interactive)` that iterates `plan["tasks_to_create"]` and calls `task-insert`.
- Reuse the existing task insertion shape from `_seed_bootstrap_tasks`; include `--domain` only if a proposal supplies it.
- Call `_seed_tasks_from_plan` when `plan["actions"]["materialize"] and seed_plan_tasks == "all"`.
- Combine plan-seeded tasks and bootstrap-seeded tasks in the existing `seeded_tasks`/`skipped_tasks` response arrays.

- [ ] **Step 4: Run wizard tests and verify GREEN**

Run: `python3 -m pytest tests/integration/test_init_wizard.py::test_seed_plan_tasks_materializes_vertical_slice_task tests/integration/test_init_wizard.py::test_seed_plan_tasks_skip_materialization_writes_no_tasks -q`

Expected: both tests pass.

### Task 4: Docs, Help, And Dispatcher Registration

**Files:**
- Modify: `bin/tusk`
- Modify: `bin/tusk-init-wizard.py`
- Modify: `docs/SCRIPTS.md`
- Modify: `docs/DOMAIN.md`
- Modify: `skills/tusk-init/SKILL.md`
- Modify: `codex-prompts/tusk-init.md` if present
- Test: existing help and prompt tests

- [ ] **Step 1: Add failing coverage for CLI/help/doc expectations where existing tests support it**

Run existing tests first to identify exact assertions:

`python3 -m pytest tests/unit/test_codex_tusk_prompt_upgrade_bootstrap.py tests/integration/test_init_wizard.py::test_init_wizard_help_documents_new_flags -q`

If a help test exists, extend it to assert `--seed-plan-tasks`, `--plan-task-mode`, `--plan-task-id`, `--plan-remove-task`, and `--plan-add-task`.

- [ ] **Step 2: Register and document new command/flags**

Update:

- `bin/tusk` command list and dispatcher with `init-vertical-slice`.
- `bin/tusk-init-wizard.py` module docstring with the new flags.
- `docs/SCRIPTS.md` to list `tusk-init-vertical-slice.py` and mention plan task seeding.
- `docs/DOMAIN.md` bootstrap plan section to describe vertical-slice task proposals and task-level controls.
- `skills/tusk-init/SKILL.md` and `codex-prompts/tusk-init.md` to explain review, pick/edit/skip, and accepted materialization.

- [ ] **Step 3: Run focused docs/help tests**

Run: `python3 -m pytest tests/unit/test_codex_tusk_prompt_upgrade_bootstrap.py tests/integration/test_init_wizard.py::test_init_wizard_help_documents_new_flags -q`

Expected: pass, or skip prompt test if the file/test is absent.

### Task 5: Final Verification And Task Closeout

**Files:**
- All changed files

- [ ] **Step 1: Run focused unit tests**

Run: `python3 -m pytest tests/unit/test_init_vertical_slice.py tests/unit/test_init_bootstrap_plan.py -q`

Expected: pass.

- [ ] **Step 2: Run focused integration tests**

Run: `python3 -m pytest tests/integration/test_init_wizard.py -q`

Expected: pass.

- [ ] **Step 3: Run configured unit suite**

Run: `python3 -m pytest tests/unit/ -q`

Expected: pass.

- [ ] **Step 4: Mark criteria done**

Use `TUSK_DB=/Users/mattgioe/Desktop/projects/tusk/tusk/tasks.db ./bin/tusk criteria done <id>` for criteria 3538, 3539, 3540, and 3541 after the relevant tests pass.

- [ ] **Step 5: Commit implementation**

Use `TUSK_DB=/Users/mattgioe/Desktop/projects/tusk/tusk/tasks.db ./bin/tusk commit 760 "Generate vertical-slice init tasks" <changed files> --criteria 3538 --criteria 3539 --criteria 3540 --criteria 3541`.

- [ ] **Step 6: Review and merge**

Run Tusk review/merge workflow for TASK-760 from the task worktree after verification is green.
