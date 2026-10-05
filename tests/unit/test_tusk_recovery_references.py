"""Resolve conditional recovery references in source and shipped layouts."""

import importlib.util
import json
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
SECTIONS = {
    "deliverable-check",
    "stalled-exploration",
    "stalled-implementation",
    "commit-recovery",
    "merge-recovery",
}
SURFACES = (
    ("skills/tusk/SKILL.md", "RECOVERY.md", 9960),
    ("codex-prompts/tusk.md", "tusk-recovery.md", 7948),
)


def assert_references_resolve(core, companion_name):
    text = core.read_text(encoding="utf-8")
    links = re.findall(r"\]\((" + re.escape(companion_name) + r")#([a-z-]+)\)", text)
    assert {anchor for _, anchor in links} == SECTIONS
    for filename, anchor in links:
        companion = core.parent / filename
        content = companion.read_text(encoding="utf-8")
        assert len(re.findall(r'<a id="' + re.escape(anchor) + r'"></a>', content)) == 1


@pytest.mark.parametrize("core_name,companion_name,baseline_words", SURFACES)
def test_source_recovery_links_resolve(core_name, companion_name, baseline_words):
    assert_references_resolve(ROOT / core_name, companion_name)


@pytest.mark.parametrize("core_name,companion_name,baseline_words", SURFACES)
def test_initial_context_is_smaller_without_loading_companions(core_name, companion_name, baseline_words):
    core = (ROOT / core_name).read_text(encoding="utf-8")
    assert len(core.split()) < baseline_words
    assert (ROOT / core_name).with_name(companion_name).stat().st_size > 0


@pytest.fixture(scope="module")
def upgrade():
    spec = importlib.util.spec_from_file_location("recovery_upgrade", ROOT / "bin/tusk-upgrade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_installs_resolvable_companions_in_both_layouts(tmp_path, upgrade):
    upgrade.copy_skills(str(ROOT), str(tmp_path))
    upgrade.copy_prompts(str(ROOT), str(tmp_path))
    assert_references_resolve(tmp_path / ".claude/skills/tusk/SKILL.md", "RECOVERY.md")
    assert_references_resolve(tmp_path / ".codex/prompts/tusk.md", "tusk-recovery.md")


def test_manifest_owns_both_installed_companions():
    manifest = json.loads((ROOT / "MANIFEST").read_text())
    assert ".claude/skills/tusk/RECOVERY.md" in manifest
    assert ".codex/prompts/tusk-recovery.md" in manifest
    assert ".codex/prompts/tusk-recovery-canonical.md" in manifest


def test_generated_mirror_keeps_installed_codex_fallback(tmp_path, upgrade):
    # Existing generated mirrors may have SKILL.md but no new companion.
    # Exercise the real text transformation and distribution path rather than
    # assuming upgrade creates new mirror-owned files.
    upgrade.copy_prompts(str(ROOT), str(tmp_path))
    transformed = upgrade._codex_skill_text((ROOT / "skills/tusk/SKILL.md").read_text())
    fallback = ".codex/prompts/tusk-recovery-canonical.md"
    assert fallback in transformed
    installed = tmp_path / fallback
    assert installed.read_bytes() == (ROOT / "skills/tusk/RECOVERY.md").read_bytes()
    content = installed.read_text()
    for anchor in SECTIONS:
        assert f'<a id="{anchor}"></a>' in content


def test_canonical_mirror_never_routes_to_codex_variant():
    canonical = (ROOT / "skills/tusk/SKILL.md").read_text()
    assert ".codex/prompts/tusk-recovery.md" not in canonical
    codex = (ROOT / "codex-prompts/tusk.md").read_text()
    assert ".codex/prompts/tusk-recovery-canonical.md" not in codex


def test_canonical_distribution_copy_stays_identical():
    assert (ROOT / "codex-prompts/tusk-recovery-canonical.md").read_bytes() == (
        ROOT / "skills/tusk/RECOVERY.md"
    ).read_bytes()
