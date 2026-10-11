"""Native skill ownership, discovery and complete release bundles."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location('native_skills', ROOT / 'bin/tusk-skills.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def package(tmp_path):
    module = load()
    bundle = tmp_path / 'bundle'
    shutil.copytree(ROOT / 'codex-skills', bundle)
    project = tmp_path / 'project'
    project.mkdir()
    return module, bundle, project


def test_core_and_optional_groups(package):
    m, bundle, root = package
    result = m.reconcile(root, bundle)
    assert not result['issues']
    assert (root / '.agents/skills/tusk/references/runtime.md').is_file()
    assert (root / '.agents/skills/tusk/references/recovery.md').is_file()
    assert not (root / '.agents/skills/loop/SKILL.md').exists()
    assert not m.reconcile(root, bundle, check=True)['issues']
    assert not m.reconcile(root, bundle)['changed']
    m.reconcile(root, bundle, groups=['core', 'orchestration', 'reporting', 'integrations'])
    assert (root / '.agents/skills/loop/SKILL.md').is_file()
    assert not m.discovery_issues(root, include_user=False)
    # Bundle entry points stay small; references carry shared mechanics.
    for entry in (root / '.agents/skills').glob('*/SKILL.md'):
        assert len(entry.read_text().splitlines()) < 65


def test_upgrade_preserves_customizations_and_repairs_missing(package):
    m, bundle, root = package
    custom = root / '.agents/skills/tusk/SKILL.md'
    custom.parent.mkdir(parents=True)
    custom.write_text('---\nname: tusk\n---\nMy custom task workflow\n')
    original = custom.read_bytes()
    assert m.reconcile(root, bundle)['issues']
    assert custom.read_bytes() == original
    assert not (custom.parent / 'references').exists()
    managed = root / '.agents/skills/create-task/SKILL.md'
    modified = managed.read_text() + '\nLocal addition\n'
    managed.write_text(modified)
    missing = root / '.agents/skills/resume-task/SKILL.md'
    missing.unlink()
    (bundle / 'resume-task/SKILL.md').write_text((bundle / 'resume-task/SKILL.md').read_text() + '\nRelease update\n')
    m.reconcile(root, bundle)
    assert managed.read_text() == modified
    assert missing.read_bytes() == (bundle / 'resume-task/SKILL.md').read_bytes()
    assert any('local modification' in issue for issue in m.reconcile(root, bundle, check=True)['issues'])


def test_retirement_and_symlinks(package, tmp_path):
    m, bundle, root = package
    m.reconcile(root, bundle, groups=['core', 'reporting'])
    custom = root / '.agents/skills/cost/SKILL.md'
    custom.write_text('custom cost workflow')
    result = m.reconcile(root, bundle, groups=['core'])
    assert custom.read_text() == 'custom cost workflow'
    assert any('custom retired' in issue for issue in result['issues'])
    assert not (root / '.agents/skills/groom-backlog/SKILL.md').exists()
    outside = tmp_path / 'outside'
    outside.mkdir()
    project = tmp_path / 'symlink-project'
    project.mkdir()
    (project / '.agents').symlink_to(outside, target_is_directory=True)
    assert m.reconcile(project, bundle)['issues']
    assert not list(outside.iterdir())


def test_health_and_corrupt_state(package):
    m, bundle, root = package
    m.reconcile(root, bundle)
    entry = root / '.agents/skills/tusk/references/runtime.md'
    entry.unlink()
    assert any('broken reference' in issue for issue in m.discovery_issues(root, include_user=False))
    duplicate = root / '.agents/skills/other/SKILL.md'
    duplicate.parent.mkdir()
    duplicate.write_text('---\nname: tusk\n---\nOther\n')
    assert any('duplicate skill name tusk' in issue for issue in m.discovery_issues(root, include_user=False))
    (root / m.STATE).write_text('{broken')
    with pytest.raises(ValueError):
        m.reconcile(root, bundle)
    assert not entry.exists()


def test_worktree_selection(package, tmp_path):
    m, bundle, root = package
    m.reconcile(root, bundle, groups=['core', 'reporting'])
    workspace = tmp_path / 'worktree'
    workspace.mkdir()
    m.seed_worktree(root, workspace, bundle)
    assert (workspace / '.agents/skills/cost/SKILL.md').is_file()
    assert not m.discovery_issues(workspace, include_user=False)


def test_proven_legacy_mirror_migrates(package):
    m, bundle, root = package
    rel = '.agents/skills/tusk/SKILL.md'
    target = root / rel
    target.parent.mkdir(parents=True)
    old = b'old generated mirror'
    target.write_bytes(old)
    (root / 'tusk').mkdir()
    (root / 'tusk/codex-skill-mirrors.json').write_text(json.dumps({
        'version': 1, 'files': {rel: m.digest(old)}}))
    assert not m.reconcile(root, bundle)['issues']
    assert target.read_bytes() == (bundle / 'tusk/SKILL.md').read_bytes()
    assert rel in m.state(root)['files']
