"""Regression coverage for address-issue release-boundary guidance."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDANCE_PATHS = (
    REPO_ROOT / "skills" / "address-issue" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "address-issue.md",
)
START = "<!-- release-boundary-check:start -->"
END = "<!-- release-boundary-check:end -->"


def _release_boundary_section(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"{re.escape(START)}\n(.*?)\n{re.escape(END)}",
        text,
        re.DOTALL,
    )
    assert match is not None, f"{path} must include marked release-boundary guidance"
    return " ".join(match.group(1).split())


def test_skill_and_codex_prompt_share_release_boundary_guidance():
    sections = [_release_boundary_section(path) for path in GUIDANCE_PATHS]

    assert sections[0] == sections[1]


def test_release_boundary_guidance_uses_fail_closed_git_ancestry():
    section = _release_boundary_section(GUIDANCE_PATHS[0])
    semantic = section.replace("**", "").replace("`", "")

    required = (
        'git log -1 --format=%H "$RELEASE_REF" -- VERSION',
        'git merge-base --is-ancestor "$FIX_COMMIT" "$RELEASE_REF"',
        'git merge-base --is-ancestor "$FIX_COMMIT" "$RELEASE_COMMIT"',
        "upgrade-available",
        "source-resolved but undistributed",
        "Do not describe the issue as consumer-available",
        "recommend a VERSION bump/release",
        "availability indeterminate",
        "Fail closed",
        "direct invocation that still reproduces the failure overrides",
        "incomplete multi-commit fix",
    )
    for phrase in required:
        assert phrase in semantic
