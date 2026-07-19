"""Regression tests for safe ``tusk commit`` exit-9 recovery guidance."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _exit9_block(relpath: str) -> str:
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    start = text.index("**If `tusk commit` exits 9")
    end = text.index("**If `tusk commit` fails with `pathspec", start)
    return " ".join(text[start:end].split())


def test_tusk_workflows_condition_exit9_retry_on_observed_state():
    for relpath in ("skills/tusk/SKILL.md", "codex-prompts/tusk.md"):
        block = _exit9_block(relpath)

        assert "TUSK_COMMIT_RESULT" in block
        assert "Retry only when" in block
        assert "requested commit did not land" in block
        assert "do not reissue `tusk commit`" in block
        assert "investigate instead of retrying blindly" in block


def test_tusk_workflows_document_exit9_fallback_state_checks():
    for relpath in ("skills/tusk/SKILL.md", "codex-prompts/tusk.md"):
        block = _exit9_block(relpath)

        assert "git log -1 --format='%H %s'" in block
        assert 'git status --short -- "<file1>"' in block
        assert "tusk criteria list <id>" in block
        assert "criterion bindings landed" in block
