"""Regression coverage for create-task task-type classification guidance."""

import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SURFACES = (
    os.path.join(REPO_ROOT, "skills", "create-task", "SKILL.md"),
    os.path.join(REPO_ROOT, "codex-prompts", "create-task.md"),
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return " ".join(fh.read().lower().split())


def test_additive_fix_gap_wording_is_classified_as_feature():
    for path in SURFACES:
        body = _read(path)
        assert "classify behavior from its contract, not its verbs" in body
        assert (
            "use `feature` when the request adds behavior or capability that the "
            "existing contract does not promise"
        ) in body
        assert '"fix the gap"' in body


def test_existing_contract_violations_and_regressions_remain_bugs():
    for path in SURFACES:
        body = _read(path)
        assert "violates an existing requirement or contract" in body
        assert "regresses previously working behavior" in body
        assert 'the word "fix" is not evidence of a defect by itself' in body


def test_ambiguous_type_is_explicitly_reviewed_before_insertion():
    for path in SURFACES:
        body = _read(path)
        assert body.count("classification note:") >= 3
        assert "contract evidence or missing evidence" in body
        assert "confirm or edit that proposed type before insertion" in body
        assert "unless the operator edits it" in body
