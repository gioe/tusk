from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_codex_tusk_prompt_upgrades_and_reloads_before_workflow():
    prompt = (REPO_ROOT / "codex-prompts" / "tusk.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "tusk upgrade --no-commit" in normalized
    assert "read the current `.codex/prompts/tusk.md` from disk exactly once" in normalized
    assert "whether the command reports `Upgrade complete` or `Already up to date`" in normalized
    assert "do not continue from the stale prompt text" in normalized
    assert "do not repeat this upgrade/reload bootstrap again" in normalized


def test_claude_tusk_skill_upgrades_and_reloads_before_workflow():
    skill = (REPO_ROOT / "skills" / "tusk" / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(skill.split())

    assert "tusk upgrade --no-commit" in normalized
    assert "read the current `.claude/skills/tusk/SKILL.md` from disk exactly once" in normalized
    assert "whether the command reports `Upgrade complete` or `Already up to date`" in normalized
    assert "do not continue from the stale skill text" in normalized
    assert "do not repeat this upgrade/reload bootstrap again" in normalized
