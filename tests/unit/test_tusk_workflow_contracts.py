"""Contract checks for mirrored workflow guidance, not agent performance evals."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ("skills/tusk/SKILL.md", "codex-prompts/tusk.md")


@pytest.fixture(params=WORKFLOWS)
def workflow(request):
    return " ".join((ROOT / request.param).read_text().split())


def test_lint_contract_distinguishes_blocking_rules(workflow):
    standalone = workflow.split("10. **Run convention lint", 1)[1].split("10b.", 1)[0]
    assert "Lint runs at merge time" in workflow
    assert "`tusk commit` does not run lint" in workflow
    assert "`--skip-lint` is ignored by `tusk commit`" in workflow
    assert "advisory warnings do not block" in standalone
    assert "blocking violations" in standalone
    assert "advisory only" not in standalone
    assert "This runs `tusk lint`" not in workflow
    assert "`tusk commit` exits 6" not in workflow
    assert "`tusk merge` exits 6" in workflow


def test_finalization_accepts_only_documented_success_outcomes(workflow):
    finalization = workflow.split("**Finalization outcome gate:**", 1)[1].split("```", 1)[0]
    assert "merge exit 0" in finalization
    assert "merge exit 3 (partial cleanup)" in finalization
    assert "abandon exit 0" in finalization
    assert "For every other exit code, stop finalization" in finalization
    assert "stable checkout" in finalization
    assert "Only after `tusk merge` (or `tusk abandon`) exits 0" not in workflow


def test_all_raw_commit_examples_use_runtime_attribution(workflow):
    trailers = re.findall(r'--trailer "([^"]+)"', workflow)
    assert trailers, "Exercise actual fallback commit examples"
    assert all(t == "Co-Authored-By: <executing agent name and email>" for t in trailers)
    assert "Replace the attribution placeholder" in workflow
    assert "do not copy a historical model identity" in workflow
    assert "If the identity is unavailable, omit the trailer" in workflow
