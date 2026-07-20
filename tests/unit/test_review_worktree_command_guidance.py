"""Regression coverage for review workflow Tusk-wrapper resolution."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDANCE_PATHS = (
    REPO_ROOT / "skills" / "review-commits" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "review-commits.md",
)
ADDRESS_GUIDANCE_PATHS = (
    REPO_ROOT / "skills" / "address-issue" / "SKILL.md",
    REPO_ROOT / "codex-prompts" / "address-issue.md",
)
BLOCK_HEADING = "### Resolve the Tusk wrapper for this checkout"


def _resolver_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    section = text.split(BLOCK_HEADING, 1)[1]
    match = re.search(r"```bash\n(.*?)\n```", section, re.DOTALL)
    assert match is not None
    return match.group(1)


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_canonical_and_codex_review_guidance_share_the_resolver():
    blocks = [_resolver_block(path) for path in GUIDANCE_PATHS]

    assert blocks[0] == blocks[1]
    assert '"$REVIEW_REPO_ROOT/bin/tusk"' in blocks[0]
    assert '"$REVIEW_REPO_ROOT/tusk/bin/tusk"' in blocks[0]
    assert '"$REVIEW_REPO_ROOT/.claude/bin/tusk"' in blocks[0]
    assert blocks[0].index("/bin/tusk") < blocks[0].index("/tusk/bin/tusk")
    assert blocks[0].index("/tusk/bin/tusk") < blocks[0].index("/.claude/bin/tusk")


def test_review_and_address_issue_share_source_worktree_wrapper_contract():
    review_block = _resolver_block(GUIDANCE_PATHS[0])

    assert '"$REVIEW_REPO_ROOT/bin/tusk"' in review_block
    for path in ADDRESS_GUIDANCE_PATHS:
        text = path.read_text(encoding="utf-8")
        assert 'ADDRESS_ISSUE_WORKTREE_TUSK_BIN="./bin/tusk"' in text


@pytest.mark.parametrize(
    "wrapper_path",
    ("bin/tusk", "tusk/bin/tusk", ".claude/bin/tusk"),
)
def test_documented_resolver_selects_an_executable_checkout_wrapper(
    tmp_path: Path, wrapper_path: str
):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    expected = tmp_path / wrapper_path
    _make_executable(expected)

    result = subprocess.run(
        ["bash", "-c", _resolver_block(GUIDANCE_PATHS[0])],
        cwd=tmp_path,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )

    assert Path(result.stdout.strip()).resolve() == expected.resolve()


def test_review_guidance_overrides_fixed_invocation_wrapper_paths():
    for path in GUIDANCE_PATHS:
        text = path.read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        assert "Capture the printed absolute path as `REVIEW_TUSK_BIN`" in text
        assert "do not assume the shell variable or a shell function persists" in normalized
        assert "Do not continue using a fixed wrapper path supplied by the invocation wrapper" in normalized
