"""Contract coverage for plan/run/full objective workflow guidance."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OBJECTIVE_SURFACES = (
    ROOT / "skills/objective/SKILL.md",
    ROOT / "codex-prompts/objective.md",
)
CREATE_TASK_SURFACES = (
    ROOT / "skills/create-task/SKILL.md",
    ROOT / "codex-prompts/create-task.md",
)


def test_objective_surfaces_expose_plan_run_full_and_bare_intent_compatibility():
    for path in OBJECTIVE_SURFACES:
        body = path.read_text(encoding="utf-8")
        assert "objective plan OBJ-N" in body
        assert "objective run OBJ-N" in body
        assert "objective full" in body
        assert "backward-compatible alias" in body
        assert "Do not invoke" in body


def test_objective_surfaces_use_atomic_import_results_without_id_windows():
    for path in OBJECTIVE_SURFACES:
        body = path.read_text(encoding="utf-8")
        assert "objective-aware" in body
        assert "created.*.task_id" in body
        assert "skipped.*.matched_task_id" in body
        assert "`--best-effort`" in body
        assert "BEFORE_MAX" not in body
        assert "MAX(id)" not in body
        assert "WHERE id >" not in body


def test_create_task_surfaces_define_objective_planning_persistence_contract():
    for path in CREATE_TASK_SURFACES:
        body = path.read_text(encoding="utf-8")
        assert "OBJECTIVE_ID" in body
        assert "duplicate_policy" in body
        assert "skipped.*.matched_task_id" in body
        assert "`--best-effort`" in body
        assert "maximum-ID" in body
