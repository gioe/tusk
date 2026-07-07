# Bootstrap Pack Examples

This directory contains starter contracts for utility repos that publish `tusk-bootstrap.json`.

Use these files as source templates when creating or updating companion repos:

- `ios-libs/tusk-bootstrap.json` is a schema-v2 example for `gioe/ios-libs`.
- `android-libs/CONTRACT.md` describes the expected future Android pack.
- `web-libs/CONTRACT.md` describes the expected future web pack.
- `backend-libs/CONTRACT.md` describes the expected future backend pack.

The JSON examples are validated by `tests/unit/test_bootstrap_pack_examples.py` with the same validator used by `tusk init-fetch-bootstrap`.

Future repos may be absent from the built-in selector catalog. When a matching optional pack has no configured repo, `tusk init-bootstrap-select` reports it under `skipped_modules` instead of failing project initialization.
