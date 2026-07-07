# Safe Bootstrap Materialization Design

## Goal

`tusk-init` should apply confirmed bootstrap-plan files safely enough to generate useful starter software without silently overwriting user-owned code.

## Approach

Extend the existing `tusk init-write-manifest-files` utility instead of introducing a second materializer. The command already owns path validation, create-only writes, append-if-missing snippets, and `add-lib` integration, so the safest path is to deepen that contract in place.

The writer will support:

- `create_only`: create missing files and skip existing files.
- `append_if_missing`: append content only when the exact rendered content is absent.
- `marker_block`: create or replace a bounded managed section between explicit begin/end markers.
- template rendering from a confirmed init intent JSON model.
- dry-run output that reports the same write/skip/conflict decisions without mutating the filesystem.

## Safety Contract

Existing files are never overwritten by default. `create_only` skips any existing path. `append_if_missing` only appends missing content. `marker_block` is the only mode allowed to update existing content, and it can update only text between caller-provided marker strings. If a marker entry has no markers, only one marker, or a file contains only one side of the marker pair, the writer reports a conflict and exits without changing that entry.

Template rendering is intentionally small and deterministic. Manifest authors can reference values using `{{ key }}` or dotted paths such as `{{ init_intent.name }}`. Missing variables are conflicts, not empty strings, so generated files do not hide bad manifests.

Dry-run mode evaluates paths, templates, and file state exactly like a real run, but returns planned `wrote`, `skipped`, and `conflicts` arrays without creating directories or files.

## Validation And Docs

Bootstrap manifest validation should accept the new `marker_block` mode and require marker strings for that mode. Documentation should describe all modes, template context, dry-run behavior, and conflict reporting so utility repo maintainers know what they can safely ship.
