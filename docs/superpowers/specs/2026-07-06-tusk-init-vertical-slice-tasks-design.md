# Tusk Init Vertical-Slice Tasks Design

## Goal

Make `tusk-init` propose the first useful product slice during project setup, not only generic setup work. The generated backlog should connect the user's stated workflow, archetype, selected starter modules, data needs, integrations, tests, and documentation into shippable tasks that a later `/tusk` run can execute without the original conversation.

This advances the init objective by making the bootstrap plan a durable project contract: the user states intent once, reviews the proposed plan, and accepts concrete starter work that carries that intent into the task database.

## Scope

This change extends the existing bootstrap-plan path. It does not create a second task planning system, a freeform LLM planner, or a new project scaffolder. The plan remains deterministic, reviewable JSON built from confirmed init inputs.

In scope:

- Generate first vertical-slice task proposals from project intent, archetype, workflow, data, integrations, selected utility modules, and quality goals.
- Include behavior and verification acceptance criteria, not only setup criteria.
- Let users accept, edit, pick, or skip generated task proposals before materialization.
- Seed accepted plan tasks into the backlog during `tusk-init` when materialization is explicitly accepted.
- Cover mobile, web, and backend proposal shapes in tests.

Out of scope:

- Executing generated tasks during init.
- Generating app code beyond existing scaffold utilities.
- Calling external utility repos at proposal time beyond the existing bootstrap/library fetch path.
- Replacing `/create-task`; this is a focused init-time starter backlog generator.

## Architecture

Add a pure generator module for vertical-slice task proposals. The module accepts normalized plan inputs and returns task dictionaries with stable IDs, summaries, descriptions, criteria, task type, domain hints, and a source marker such as `vertical_slice:<archetype>`.

`bin/tusk-init-bootstrap-plan.py` remains the composition point. After it selects utility modules and ordinary bootstrap tasks, it asks the vertical-slice generator for starter tasks and appends them to `tasks_to_create`. This keeps one user-reviewable plan object as the source of truth.

`bin/tusk-init-wizard.py` remains the materialization point. It forwards task-selection flags to the plan builder and, when the accepted plan requests materialization, inserts accepted plan tasks through `task-insert`. Non-interactive callers must explicitly accept materialization before any tasks are written.

## Proposal Rules

The generator should use conservative templates keyed by project family:

- Mobile/iOS: build the first app workflow with screen behavior, local or remote data handling, selected starter modules, tests, and docs.
- Web/dashboard: build the first route or workspace flow with UI behavior, state/data access, integrations, tests, and docs.
- Backend/API: build the first endpoint, job, or service flow with schema/data behavior, integration boundaries, API tests, and docs.

Inputs should be treated as hints, not exhaustive specs. Missing inputs should produce useful generic language instead of blocking init. For example, an unknown workflow can become "first product workflow"; an unknown entity can become "core project entity".

Every generated task should include criteria that cover:

- Observable user or API behavior.
- Data/entity handling.
- Integration or selected-module wiring when applicable.
- Automated verification.
- Short project documentation or handoff context.

## User Controls

The plan path should support task-level control before materialization:

- Accept all generated tasks by accepting the plan.
- Skip generated tasks by selecting a no-task mode or skipping materialization.
- Pick specific generated task IDs.
- Remove specific generated task IDs.
- Add or replace task JSON for edited proposals.

These controls should exist as CLI flags so both the interactive wizard and tests can exercise the same behavior. The JSON plan should expose stable generated task IDs so a user or agent can make precise edits.

## Materialization

Plan task insertion should happen only after the plan is accepted. The wizard should expose an explicit `--seed-plan-tasks all|none` style gate so non-interactive setup cannot unexpectedly mutate the backlog.

When seeding, `tusk-init-wizard.py` should call `task-insert` for each accepted plan task and pass through criteria from the task proposal. Duplicate handling should follow the existing task insertion semantics rather than inventing a new duplicate detector.

The wizard response should report which plan tasks were seeded and which were skipped or rejected, matching the existing bootstrap task reporting style.

## Error Handling

Generation should be side-effect free and tolerant of sparse inputs. Invalid task-edit JSON should fail before materialization with a clear error. Pick/remove references to unknown generated IDs should fail in the plan-building phase so the accepted plan cannot silently diverge from the user's choices.

Task insertion failures should be reported per task while allowing the wizard to return a structured result. This mirrors the current bootstrap task seeding behavior and keeps init debuggable.

## Testing

Add focused tests for the pure generator and plan composition:

- Mobile proposal includes a workflow-oriented task, behavior criteria, data criteria, verification, and docs.
- Web proposal includes a route/workspace-oriented task, behavior criteria, integration or data criteria, verification, and docs.
- Backend proposal includes endpoint or service behavior, schema/data criteria, integration boundaries, API verification, and docs.
- Plan controls can pick, remove, skip, and add task proposals by stable ID.
- Wizard materialization can seed accepted plan tasks and can skip them when materialization is declined.

Focused unit tests should cover deterministic generation. One integration-style wizard test should prove accepted plan tasks become real backlog rows with criteria.

## Product Fit

This design supports the Accessible pillar by making project setup produce immediately actionable work. It supports the Opinionated pillar by encoding a good first-slice shape instead of leaving users with generic setup chores. It supports the Transparent context handoff model by writing the user's init intent into concrete tasks and criteria that future agents can resume from.
