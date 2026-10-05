"""Retro must preserve executable evidence in its task-creation handoff."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATHS = ("skills/retro/SKILL.md", "skills/retro/FULL-RETRO.md", "codex-prompts/retro.md")


@pytest.mark.parametrize("path", PATHS)
def test_followup_insertion_preserves_typed_specs_and_manual_judgment(path):
    text = (ROOT / path).read_text()
    section = text.split("**project-issues** —", 1)[1].split("###", 1)[0]
    normalized = " ".join(section.split())
    assert "at least one `--criteria` or `--typed-criteria`" in normalized
    assert "Always include at least one `--criteria` flag" not in normalized
    for kind in ("`test`", "`code`", "`file`"):
        assert kind in normalized
    assert "manual criteria for requirements that need human judgment" in normalized
    assert "exit 0 means the condition is satisfied" in normalized
    assert "Do not replace a known executable spec with a manual criterion" in normalized
    assert "tusk typed-criteria-build" in section
    assert "--spec-file" in section
    assert '--typed-criteria "$RETRO_TYPED"' in section
    assert "|| exit" in section
    assert "non-interpolating file write" in normalized
    assert "all-typed task needs no manual placeholder" in normalized
