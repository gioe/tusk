# Starter Bootstrap Packs Design

## Goal

Give utility-repo maintainers concrete starter pack contracts that prove Tusk's modular bootstrap schema without requiring this task to mutate external repos.

## Approach

Keep the deliverable local to the Tusk source repo. Add `docs/bootstrap-packs/` with a rich `ios-libs` example manifest and placeholder contracts for future `android-libs`, `web-libs`, and `backend-libs` repos. The examples should be valid against the real `tusk-bootstrap.json` validator so they can serve as copyable starting points for utility repos.

## Pack Content

The iOS example should exercise realistic starter capabilities:

- Shared design-system setup through SharedKit.
- API client wiring through APIClient.
- Navigation shell and persistence scaffolding.
- Observability and test-scaffolding modules.
- First-workflow tasks and verification hints.

The placeholder contracts should explain the expected repo-owned `tusk-bootstrap.json` shape for Android, web, and backend packs while making clear those repos are optional and currently unavailable until configured.

## Safety

The examples should use safe materialization modes: `create_only` for new project-owned files, `append_if_missing` for additive snippets, and `marker_block` for managed sections inside user-owned manifests. Future repos that are not configured should keep flowing through existing selector behavior as skipped optional packs rather than hard failures.
