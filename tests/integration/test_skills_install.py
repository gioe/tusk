"""Integration tests for project_type-gated skill installation (TASK-255).

Skills can declare an `applies_to_project_types` frontmatter field. Skills
with the field install only when the target project's
`tusk/config.json:project_type` matches one of the listed types. Universal
skills (no field) always install; gated skills are deferred when the field
is unset.

These tests inject a synthetic `_test_ios_only` skill into a tmp copy of the
source repo (so we don't pollute the real `skills/` tree) and then run
install.sh end-to-end against fresh consumer projects under different
`project_type` settings.
"""

import json
import os
import shutil
import subprocess

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TESTS_DIR))


_TOP_LEVEL_IGNORE = {
    ".git",
    "tusk",            # runtime DB dir at REPO_ROOT/tusk/ — distinct from skills/tusk/
    "tests",
    "node_modules",
    ".pytest_cache",
    ".claude",         # symlink-y tree; we copy .claude/hooks back below
}
_ANYWHERE_DIR_IGNORE = {"__pycache__"}
_FILE_GLOBS = {"*.pyc"}


def _ignore_for_copytree(dirpath, names):
    """Custom shutil.copytree ignore.

    `tusk` and friends only get skipped at the REPO_ROOT level — never from
    inside `skills/`, so the universal /tusk skill itself still copies. Names
    matching `bin/tusk` (a file with no extension) are likewise preserved.
    """
    from fnmatch import fnmatch
    skip = set()
    abs_dir = os.path.realpath(dirpath)
    is_top = abs_dir == os.path.realpath(REPO_ROOT)
    for name in names:
        full = os.path.join(dirpath, name)
        is_dir = os.path.isdir(full)
        if is_dir and name in _ANYWHERE_DIR_IGNORE:
            skip.add(name)
            continue
        if is_top and is_dir and name in _TOP_LEVEL_IGNORE:
            skip.add(name)
            continue
        for pat in _FILE_GLOBS:
            if fnmatch(name, pat):
                skip.add(name)
                break
    return skip


def _copy_install_source(dst):
    """Copy the parts of REPO_ROOT install.sh needs into dst.

    Skips heavy/unnecessary trees (.git, tests/, tusk runtime, caches) so the
    fixture is fast even though install.sh is invoked end-to-end.
    """
    shutil.copytree(REPO_ROOT, dst, ignore=_ignore_for_copytree, symlinks=False)
    # install.sh's manifest builder reads .claude/hooks; copy hook scripts
    # explicitly since the .claude/ ignore pattern excludes the symlink-y
    # skills tree.
    src_hooks = os.path.join(REPO_ROOT, ".claude", "hooks")
    dst_hooks = os.path.join(dst, ".claude", "hooks")
    if os.path.isdir(src_hooks):
        shutil.copytree(src_hooks, dst_hooks)
    # The tarball MANIFEST is read by tusk-upgrade and the rule18 self-check;
    # tusk-manifest.json is its sibling. install.sh writes the per-target
    # version itself, but having the source one available avoids spurious
    # warnings.
    src_manifest = os.path.join(REPO_ROOT, ".claude", "tusk-manifest.json")
    if os.path.isfile(src_manifest):
        os.makedirs(os.path.dirname(os.path.join(dst, ".claude", "tusk-manifest.json")), exist_ok=True)
        shutil.copy2(src_manifest, os.path.join(dst, ".claude", "tusk-manifest.json"))


def _add_gated_skill(src_clone, name, project_types):
    skill_dir = os.path.join(src_clone, "skills", name)
    os.makedirs(skill_dir, exist_ok=True)
    pt_list = "[" + ", ".join(project_types) + "]"
    body = (
        f"---\n"
        f"name: {name}\n"
        f"description: Test-only skill gated to {project_types}\n"
        f"allowed-tools: Bash\n"
        f"applies_to_project_types: {pt_list}\n"
        f"---\n\n# {name}\n\nTest body.\n"
    )
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(body)


@pytest.fixture()
def gated_source(tmp_path_factory):
    """A copy of the source repo with a synthetic ios_app-gated skill added."""
    src = tmp_path_factory.mktemp("tusk_src")
    src_path = str(src / "tusk")
    _copy_install_source(src_path)
    _add_gated_skill(src_path, "_test_ios_only", ["ios_app"])
    return src_path


def _run_install(src_clone, target):
    install_sh = os.path.join(src_clone, "install.sh")
    return subprocess.run(
        ["bash", install_sh],
        cwd=str(target),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture()
def consumer_universal(tmp_path):
    """Fresh consumer project with no tusk/config.json yet — project_type unset."""
    subprocess.run(
        ["git", "init", str(tmp_path)],
        capture_output=True, check=True, encoding="utf-8",
    )
    (tmp_path / ".claude").mkdir()
    return tmp_path


@pytest.fixture()
def consumer_ios_app(tmp_path):
    """Fresh consumer project pre-seeded with project_type=ios_app.

    Starts from config.default.json (which has all required keys) and
    overrides project_type so the validator pass during `tusk init` succeeds.
    """
    subprocess.run(
        ["git", "init", str(tmp_path)],
        capture_output=True, check=True, encoding="utf-8",
    )
    (tmp_path / ".claude").mkdir()
    cfg_dir = tmp_path / "tusk"
    cfg_dir.mkdir()
    with open(os.path.join(REPO_ROOT, "config.default.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["project_type"] = "ios_app"
    (cfg_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return tmp_path


def test_universal_skills_install_when_project_type_unset(gated_source, consumer_universal):
    """Skills without applies_to_project_types install regardless of project_type."""
    result = _run_install(gated_source, consumer_universal)
    assert result.returncode == 0, (
        f"install.sh failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    skills_dir = consumer_universal / ".claude" / "skills"
    # tusk skill is universal — must be present
    assert (skills_dir / "tusk" / "SKILL.md").is_file(), (
        "Universal /tusk skill must install when project_type is unset"
    )
    # ios_app-gated test skill must NOT be present (deferred until project_type is set)
    assert not (skills_dir / "_test_ios_only").exists(), (
        "ios_app-gated skill should be deferred when project_type is unset"
    )
    # Per-target manifest must reflect what actually shipped
    manifest = json.loads(
        (consumer_universal / ".claude" / "tusk-manifest.json").read_text(encoding="utf-8")
    )
    assert ".claude/skills/tusk/SKILL.md" in manifest
    assert not any(p.startswith(".claude/skills/_test_ios_only/") for p in manifest), (
        "Per-target manifest must NOT list deferred ios_app-gated skill files"
    )


def test_universal_install_logs_skipped_gated_skill(gated_source, consumer_universal):
    """install.sh announces which skills it skipped so operators can audit."""
    result = _run_install(gated_source, consumer_universal)
    assert result.returncode == 0
    assert "Skipped skill (project_type-gated): _test_ios_only" in result.stdout, (
        f"install.sh should announce skipped gated skills; stdout was:\n{result.stdout}"
    )


def test_ios_app_project_includes_gated_skills(gated_source, consumer_ios_app):
    """When tusk/config.json sets project_type=ios_app, ios_app-gated skills install."""
    result = _run_install(gated_source, consumer_ios_app)
    assert result.returncode == 0, (
        f"install.sh failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    skills_dir = consumer_ios_app / ".claude" / "skills"
    assert (skills_dir / "_test_ios_only" / "SKILL.md").is_file(), (
        "ios_app-gated skill must install when project_type=ios_app"
    )
    # Universal skills also install
    assert (skills_dir / "tusk" / "SKILL.md").is_file()
    # Per-target manifest now includes the gated skill
    manifest = json.loads(
        (consumer_ios_app / ".claude" / "tusk-manifest.json").read_text(encoding="utf-8")
    )
    assert ".claude/skills/_test_ios_only/SKILL.md" in manifest, (
        "Per-target manifest must list ios_app-gated skill files when project_type=ios_app"
    )


def test_ios_app_skill_filter_helper_directly():
    """Smoke-test the ios_app gating decision via the helper module directly.

    This guards the helper's contract independently of install.sh end-to-end so
    a regression in the filter logic surfaces with a small, fast test.
    """
    import sys
    sys.path.insert(0, os.path.join(REPO_ROOT, "bin"))
    try:
        import tusk_skill_filter as sf
    finally:
        sys.path.pop(0)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        skill_dir = os.path.join(d, "ios_only")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: ios_only\n"
                "description: x\n"
                "allowed-tools: Bash\n"
                "applies_to_project_types: [ios_app]\n"
                "---\n"
            )
        assert sf.should_install_skill(skill_dir, "ios_app") is True
        assert sf.should_install_skill(skill_dir, "android_app") is False
        assert sf.should_install_skill(skill_dir, None) is False
        assert sf.should_install_skill(skill_dir, "") is False
