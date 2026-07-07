# backend-libs Bootstrap Contract

Future repo: `gioe/backend-libs`

Project type: `backend`

The repo should publish a root-level `tusk-bootstrap.json` with:

- `version`: `1`
- `manifest_schema_version`: `2`
- `project_type`: `backend`
- `tasks`: starter tasks for adopting the backend utility package
- `modules`: modular backend capabilities selected from project intent

Recommended modules:

- `service-shell`: app factory, routing conventions, configuration loading, and health checks.
- `database`: migration layout, repository boundary, and connection lifecycle.
- `auth`: authentication middleware and principal model.
- `observability`: structured logging, metrics, tracing, and error reporting.
- `jobs`: background worker entry point and retry conventions.
- `test-scaffolding`: API tests, database fixtures, and smoke checks.

Module examples should include safe `files`, `append_operations`, `context_atoms`, `tasks`, and `verification_hints`. Use `create_only` for new service-owned files, `append_if_missing` for dependency manifests or ignore patterns, and `marker_block` for generated sections inside existing config files.

Until this repo exists and is configured in `project_libs`, Tusk should treat the matching backend pack as optional and report it under `skipped_modules`.
