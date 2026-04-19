"""Unit tests for rule18_manifest_drift and rule19_tusk_manifest_json_sync.

Both rules must emit a single trailing "Fix: run `tusk generate-manifest`."
line after their violation list so first-time hitters self-correct without
a round trip (see TASK-111).
"""

import importlib.util
import json
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


FIX_LINE = "  Fix: run `tusk generate-manifest`."


def _seed_source_repo(root, *, manifest, tusk_manifest=None, extra_scripts=()):
    """Seed a fake source-repo layout so the rule18/19 guards pass.

    manifest: list written to MANIFEST (the "on disk" set the rules read)
    tusk_manifest: list written to .claude/tusk-manifest.json (rule 19 only)
    extra_scripts: bin/tusk-*.py script basenames to create in bin/
    """
    bin_dir = os.path.join(root, "bin")
    os.makedirs(bin_dir, exist_ok=True)

    # bin/tusk stub — both rules short-circuit without it.
    open(os.path.join(bin_dir, "tusk"), "w").close()

    # Rule 18 reads bin/dist-excluded.txt; seed an empty one.
    open(os.path.join(bin_dir, "dist-excluded.txt"), "w").close()

    for script in extra_scripts:
        open(os.path.join(bin_dir, script), "w").close()

    with open(os.path.join(root, "MANIFEST"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    if tusk_manifest is not None:
        claude_dir = os.path.join(root, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        with open(os.path.join(claude_dir, "tusk-manifest.json"), "w", encoding="utf-8") as f:
            json.dump(tusk_manifest, f)


class TestRule18FixHint:
    def test_drift_output_ends_with_fix_line(self):
        """A drifted MANIFEST emits violations followed by a single Fix line."""
        with tempfile.TemporaryDirectory() as tmp:
            # tusk-foo.py lives in source tree but MANIFEST lists only a ghost entry,
            # so Rule 18 should report both a missing and an extra path.
            _seed_source_repo(
                tmp,
                manifest=[".claude/bin/ghost"],
                extra_scripts=["tusk-foo.py"],
            )
            violations = lint.rule18_manifest_drift(tmp)

        assert violations, "expected drift violations"
        assert violations[-1] == FIX_LINE
        # Fix line must appear exactly once (single trailing line, not per-violation).
        assert sum(1 for v in violations if v == FIX_LINE) == 1
        # The per-violation parentheticals stay descriptive but must not embed the fix hint.
        for v in violations[:-1]:
            assert "tusk generate-manifest" not in v

    def test_clean_manifest_emits_no_fix_line(self):
        """When MANIFEST is in sync, no violations (and therefore no Fix line) appear."""
        with tempfile.TemporaryDirectory() as tmp:
            # Expected entries for the seeded layout: the bin/tusk stub, the one
            # tusk-foo.py script, plus the three static files Rule 18 always adds.
            expected = [
                ".claude/bin/tusk",
                ".claude/bin/tusk-foo.py",
                ".claude/bin/config.default.json",
                ".claude/bin/VERSION",
                ".claude/bin/pricing.json",
            ]
            _seed_source_repo(
                tmp,
                manifest=expected,
                extra_scripts=["tusk-foo.py"],
            )
            assert lint.rule18_manifest_drift(tmp) == []


class TestRule19FixHint:
    def test_drift_output_ends_with_fix_line(self):
        """Rule 19 drift appends exactly one trailing Fix line."""
        with tempfile.TemporaryDirectory() as tmp:
            _seed_source_repo(
                tmp,
                manifest=[".claude/bin/only-in-manifest"],
                tusk_manifest=[".claude/bin/only-in-tusk-manifest"],
            )
            violations = lint.rule19_tusk_manifest_json_sync(tmp)

        assert violations, "expected sync violations"
        assert violations[-1] == FIX_LINE
        assert sum(1 for v in violations if v == FIX_LINE) == 1
        # Legacy per-violation hint must be gone — the fix hint appears only on the trailing line.
        for v in violations[:-1]:
            assert "tusk generate-manifest" not in v
            assert "bin/tusk-generate-manifest.py" not in v

    def test_clean_sync_emits_no_fix_line(self):
        """Matching MANIFEST and .claude/tusk-manifest.json produce no output."""
        with tempfile.TemporaryDirectory() as tmp:
            entries = [".claude/bin/tusk", ".claude/bin/VERSION"]
            _seed_source_repo(tmp, manifest=entries, tusk_manifest=entries)
            assert lint.rule19_tusk_manifest_json_sync(tmp) == []
