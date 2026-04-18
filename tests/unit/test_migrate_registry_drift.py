"""Guard against `def migrate_N` in bin/tusk-migrate.py without a matching
MIGRATIONS registry entry.

Regression test for TASK-81: `def migrate_53` was added without the
`(53, migrate_53)` tuple in the module-level MIGRATIONS list. `tusk migrate`
silently reported "Schema is up to date" and never invoked the new migration.
The registry is the authoritative driver — an unlinked function is a silent
no-op. These tests cross-reference the two so the next migration author gets
a loud failure instead.
"""

import importlib.util
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
MIGRATE_PATH = os.path.join(BIN, "tusk-migrate.py")

_spec = importlib.util.spec_from_file_location("tusk_migrate", MIGRATE_PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _defined_migrate_versions(path):
    """Sorted list of N where `def migrate_N(` appears in the module source."""
    with open(path) as f:
        src = f.read()
    return sorted(
        int(m.group(1)) for m in re.finditer(r"^def migrate_(\d+)\s*\(", src, re.MULTILINE)
    )


def _validate_registry(defined_versions, migrations, module):
    """Raise AssertionError with a clear message if the registry has drifted.

    Rules:
    - Every `def migrate_N` in the module must appear in MIGRATIONS as
      (N, module.migrate_N).
    - MIGRATIONS versions must be contiguous from 1 to max(defined_versions).
    """
    registered = dict(migrations)

    for n in defined_versions:
        if n not in registered:
            raise AssertionError(
                f"migrate_{n} defined but not in MIGRATIONS — "
                f"add `({n}, migrate_{n})` to the MIGRATIONS list in bin/tusk-migrate.py."
            )
        fn = getattr(module, f"migrate_{n}", None)
        if fn is not None and registered[n] is not fn:
            raise AssertionError(
                f"MIGRATIONS entry for version {n} does not reference module.migrate_{n}."
            )

    if not defined_versions:
        return
    max_n = max(defined_versions)
    expected = list(range(1, max_n + 1))
    actual = sorted(registered.keys())
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        parts = []
        if missing:
            parts.append(f"missing versions: {missing}")
        if extra:
            parts.append(f"unexpected versions: {extra}")
        raise AssertionError(
            f"MIGRATIONS versions must be contiguous from 1 to {max_n}; " + "; ".join(parts) + "."
        )


def test_migrations_registry_matches_defined_functions():
    """Every `def migrate_N(` in tusk-migrate.py is registered in MIGRATIONS."""
    defined = _defined_migrate_versions(MIGRATE_PATH)
    assert defined, "expected at least one `def migrate_N(` in bin/tusk-migrate.py"
    _validate_registry(defined, mod.MIGRATIONS, mod)


def test_drift_produces_clear_error_message():
    """Simulated drift: a defined migrate_N missing from MIGRATIONS raises a clear message."""
    defined = _defined_migrate_versions(MIGRATE_PATH)
    missing = max(defined)
    drifted = [(v, fn) for v, fn in mod.MIGRATIONS if v != missing]
    with pytest.raises(
        AssertionError,
        match=rf"migrate_{missing} defined but not in MIGRATIONS",
    ):
        _validate_registry(defined, drifted, mod)


def test_registry_contiguity_gap_is_rejected():
    """A hole in MIGRATIONS (and its matching function) must fail with a contiguity error."""
    defined = _defined_migrate_versions(MIGRATE_PATH)
    if len(defined) < 3:
        pytest.skip("need at least 3 migrations to test contiguity gap")
    gap = defined[len(defined) // 2]
    drifted_defined = [v for v in defined if v != gap]
    drifted_migrations = [(v, fn) for v, fn in mod.MIGRATIONS if v != gap]
    with pytest.raises(AssertionError, match="contiguous"):
        _validate_registry(drifted_defined, drifted_migrations, mod)
