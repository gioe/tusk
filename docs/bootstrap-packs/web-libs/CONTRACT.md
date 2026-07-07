# web-libs Bootstrap Contract

Future repo: `gioe/web-libs`

Project type: `web_app`

The repo should publish a root-level `tusk-bootstrap.json` with:

- `version`: `1`
- `manifest_schema_version`: `2`
- `project_type`: `web_app`
- `tasks`: starter tasks for adopting the web utility package
- `modules`: modular frontend capabilities selected from project intent

Recommended modules:

- `design-system`: tokens, components, layout primitives, and accessibility defaults.
- `app-shell`: routing, error boundary, loading states, and first workflow layout.
- `api-client`: typed fetcher, error normalization, and API fixture strategy.
- `auth-session`: session boundary and protected-route conventions.
- `observability`: web vitals, event logging, and user-visible error diagnostics.
- `test-scaffolding`: component tests, route tests, and first workflow fixtures.

Module examples should include safe `files`, `append_operations`, `context_atoms`, `tasks`, and `verification_hints`. Use `create_only` for new app-owned files, `append_if_missing` for package or ignore snippets, and `marker_block` for generated sections inside existing config files.

Until this repo exists and is configured in `project_libs`, Tusk should treat the matching web pack as optional and report it under `skipped_modules`.
