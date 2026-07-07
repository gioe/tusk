# android-libs Bootstrap Contract

Future repo: `gioe/android-libs`

Project type: `android_app`

The repo should publish a root-level `tusk-bootstrap.json` with:

- `version`: `1`
- `manifest_schema_version`: `2`
- `project_type`: `android_app`
- `tasks`: a list of starter integration tasks
- `modules`: modular Android starter capabilities

Recommended modules:

- `compose-design-system`: Material 3 theme, typography, spacing, and reusable Compose components.
- `api-client`: Ktor or platform HTTP client setup behind an app-owned boundary.
- `navigation-shell`: typed Compose Navigation routes for the first workflow.
- `persistence`: DataStore or Room boundary for local workflow state.
- `observability`: structured logging, crash reporting, and workflow diagnostics.
- `test-scaffolding`: JVM unit tests, Compose previews, and targeted instrumentation hooks.

Module examples should include safe `files`, `append_operations`, `context_atoms`, `tasks`, and `verification_hints`. Use `create_only` for new app-owned files, `append_if_missing` for Gradle snippets or ignore patterns, and `marker_block` for managed sections inside existing Gradle manifests.

Until this repo exists and is configured in `project_libs`, Tusk should treat the matching Android pack as optional and report it under `skipped_modules`.
