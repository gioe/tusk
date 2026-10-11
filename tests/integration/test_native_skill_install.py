"""Native Codex skills installed and repaired by real Tusk distribution flows."""
import json
from pathlib import Path
import shutil

import pytest
from tests.integration.test_git_hook_lifecycle import ROOT, git, init, install, load, run


@pytest.mark.parametrize('mode', ['codex', 'dual'])
def test_install_upgrade_and_worktree(tmp_path, monkeypatch, mode):
    repo = tmp_path / 'project'
    init(repo, mode)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    scripts = install(repo)
    entry = repo / '.agents/skills/tusk/SKILL.md'
    assert entry.is_file()
    assert (scripts / 'codex-skills/catalog.json').is_file()
    assert (repo / '.codex/prompts/tusk.md').is_file()
    git(repo, 'check-ignore', str(entry))
    run(repo, str(scripts / 'tusk'), 'skills', 'install', '--groups', 'core,reporting')
    report = repo / '.agents/skills/cost/SKILL.md'
    assert report.is_file()
    custom = repo / '.agents/skills/retro/SKILL.md'
    custom.write_text(custom.read_text() + '\nLocal customization\n')
    kept = custom.read_bytes()
    entry.unlink()
    upgrade = load('tusk-upgrade')
    stage = tmp_path / 'stage'
    stage.mkdir()
    assert upgrade.reconcile_native_skills(str(ROOT), str(repo), str(scripts), check=True)
    upgrade._run_upgrade_steps(str(ROOT), str(repo), str(scripts), str(stage))
    assert entry.is_file() and custom.read_bytes() == kept
    assert report.is_file()
    check = run(repo, str(scripts / 'tusk'), 'skills', 'check', '--format', 'json', check=False)
    assert check.returncode == 1 and 'local modification' in check.stdout
    # Preserve uncommitted installed skills in a real task workspace.
    git(repo, 'add', 'AGENTS.md')
    git(repo, 'commit', '-m', 'initial')
    inserted = run(repo, str(scripts / 'tusk'), 'task-insert', 'Native skill pickup', 'Test worktree skill discovery', '--scope', 'AGENTS.md', '--criteria', 'Skills remain discoverable')
    task = json.loads(inserted.stdout)['task_id']
    result = run(repo, str(scripts / 'tusk'), 'task-worktree', 'create', str(task), 'native-skills', '--workspace-root', str(tmp_path / 'worktrees'))
    workspace = Path(json.loads(result.stdout)['workspace_path'])
    assert (workspace / '.agents/skills/retro/SKILL.md').read_bytes() == kept
    assert (workspace / '.agents/skills/tusk/SKILL.md').is_file()
    assert (workspace / '.agents/skills/cost/references/runtime.md').is_file()
    assert '.agents/' not in git(workspace, 'status', '--porcelain').stdout
