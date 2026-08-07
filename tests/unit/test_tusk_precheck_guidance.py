"""Regression coverage for non-reproduced test-precheck guidance."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("relative_path", ["skills/tusk/SKILL.md", "codex-prompts/tusk.md"])
def test_non_reproduced_precheck_retries_original_gate(relative_path):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    branch = text.index("If `verdict` is `non_reproduced`")
    excerpt = " ".join(text[branch:branch + 900].split())

    assert "original commit-gate failure" in excerpt
    assert "Retry the same `tusk commit`" in excerpt
    assert "Do not infer that the task changes introduced" in excerpt
