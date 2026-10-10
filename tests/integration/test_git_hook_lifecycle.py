"""Git hook lifecycle contracts: real installs, upgrades, commits and pushes."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run(root, *args, check=True, input=None):
    result = subprocess.run(args, cwd=root, capture_output=True, text=True,
                            encoding="utf-8", input=input, timeout=120)
    if check:
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def git(root, *args, **kwargs):
    return run(root, "git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args, **kwargs)


def init(root, mode="codex"):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", "main")
    if mode in ("claude", "dual"):
        (root / ".claude").mkdir(exist_ok=True)
    if mode in ("codex", "dual"):
        (root / "AGENTS.md").write_text("# Instructions\n")


def install(root):
    run(root, "bash", str(ROOT / "install.sh"))
    return root / (".claude/bin" if (root / ".claude").is_dir() else "tusk/bin")


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "bin" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mode", ["claude", "codex", "dual"])
def test_contract_1(tmp_path, monkeypatch, mode):
    repo = tmp_path / mode
    init(repo, mode)
    script_dir = install(repo)
    hooks = repo / ".git/hooks"
    initial = (hooks / "pre-commit").read_bytes()
    install(repo)
    assert (hooks / "pre-commit").read_bytes() == initial
    assert not (hooks / "pre-commit.pre-tusk").exists()
    assert "version-bump-check" not in (hooks / "pre-push").read_text()
    upgrade = load("tusk-upgrade")
    monkeypatch.chdir(repo)
    staging = tmp_path / "staging"
    staging.mkdir()
    for _ in range(2):
        (hooks / "pre-commit").unlink()
        (script_dir / "hooks/git/branch-naming.sh").write_text("#!/bin/sh\nexit 0\n")
        assert upgrade._installed_git_hooks_stale(str(repo), str(script_dir))
        upgrade._run_upgrade_steps(str(ROOT), str(repo), str(script_dir), str(staging))
        assert not upgrade._installed_git_hooks_stale(str(repo), str(script_dir))
        assert not (hooks / "pre-commit.pre-tusk").exists()
        assert (script_dir / "hooks/git/branch-naming.sh").read_bytes() == (ROOT / "hooks/git/branch-naming.sh").read_bytes()
    if mode == "dual":
        assert (repo / "tusk/bin/hooks/git/branch-naming.sh").read_bytes() == (script_dir / "hooks/git/branch-naming.sh").read_bytes()


@pytest.mark.parametrize("hook_path", [None, "custom hooks", "absolute"])
def test_contract_2(tmp_path, hook_path):
    repo = tmp_path / "project"
    init(repo)
    if hook_path:
        value = str(tmp_path / "external hooks") if hook_path == "absolute" else hook_path
        git(repo, "config", "core.hooksPath", value)
    helper = load("tusk-hooks")
    hooks = helper.hook_directory(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    original = '#!/bin/sh\nprintf "%s\\n" "$@" > "$PWD/received-args"\ncat > "$PWD/received-input"\nexit 7\n'
    (hooks / "pre-push").write_text(original)
    (hooks / "pre-push").chmod(0o755)
    script_dir = install(repo)
    install(repo)
    # Every pre-push participant receives a fresh copy of the ref update stream.
    guard = script_dir / "hooks/git/branch-naming.sh"
    guard.write_text('#!/bin/sh\ncat > "$PWD/guard-input"\n')
    data = "refs/heads/main abc refs/heads/main def\n"
    result = run(repo, str(hooks / "pre-push"), "origin", "remote with spaces", input=data, check=False)
    assert result.returncode == 7
    assert (repo / "guard-input").read_text() == data
    assert (repo / "received-input").read_text() == data
    assert (repo / "received-args").read_text() == "origin\nremote with spaces\n"
    assert (hooks / "pre-push.pre-tusk").read_text() == original
    # Preserve both hooks on a collision, instead of overwriting the new one.
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 9\n")
    result = run(repo, str(script_dir / "tusk"), "hooks", "install", check=False)
    assert result.returncode != 0
    assert "collision" in result.stderr
    assert (hooks / "pre-push").read_text() == "#!/bin/sh\nexit 9\n"
    assert (hooks / "pre-push.pre-tusk").read_text() == original
    # Only the disposable test dispatcher is removed before repair.
    (hooks / "pre-push").unlink()
    run(repo, str(script_dir / "tusk"), "hooks", "install")
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-m", "initial")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature/TASK-42-linked", str(linked))
    # No installation in the linked checkout: shared dispatcher finds primary assets.
    if hook_path != "custom hooks":
        result = git(linked, "commit", "--allow-empty", "-m", "bad message", check=False)
        assert result.returncode != 0 and "Expected prefix" in result.stderr
    # Installing from a nested linked checkout handles .git files and relative paths.
    nested = linked / "nested"
    nested.mkdir()
    install(nested)
    linked_hooks = helper.hook_directory(linked)
    assert (linked_hooks / "pre-commit").is_file()
    assert not (linked / ".git").is_dir()
    linked_bin = linked / "tusk/bin/tusk"
    run(linked, str(linked_bin), "uninstall-hooks")
    assert not (linked_hooks / "pre-commit").exists()
    if hook_path != "custom hooks":
        assert (linked_hooks / "pre-push").read_text() == original


def test_contract_3(tmp_path):
    repo = tmp_path / "project"
    init(repo)
    script_dir = install(repo)
    tusk = str(script_dir / "tusk")
    hooks = repo / ".git/hooks"
    (hooks / "commit-msg").unlink()
    result = run(repo, tusk, "validate")
    assert "missing/stale dispatcher" in result.stderr and "tusk hooks install" in result.stderr
    run(repo, tusk, "hooks", "install")
    assert "Git hooks OK" in run(repo, tusk, "hooks", "check").stdout
    guard = script_dir / "hooks/git/branch-naming.sh"
    guard.write_text(guard.read_text() + "# drift\n")
    assert run(repo, tusk, "hooks", "check", check=False).returncode == 1
    # Repair from the release restores guard content too.
    install(repo)
    git(repo, "add", "AGENTS.md")
    git(repo, "commit", "-m", "initial")
    (repo / "bad.sh").write_text('sqlite3 example.db "select 1"\n')
    git(repo, "add", "bad.sh")
    result = git(repo, "commit", "-m", "bad sqlite", check=False)
    assert result.returncode != 0 and "direct sqlite3" in result.stderr
    (repo / "bad.sh").write_text("echo safe\n")
    git(repo, "add", "bad.sh")
    git(repo, "commit", "-m", "safe")
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "checkout", "-b", "bad-branch")
    result = git(repo, "push", "origin", "HEAD", check=False)
    assert result.returncode != 0 and "required pattern" in result.stderr
    git(repo, "checkout", "-b", "feature/TASK-42-valid")
    git(repo, "push", "origin", "HEAD")


def test_source_role_and_nonexecutable_user_hook(tmp_path):
    init(tmp_path)
    script_dir = install(tmp_path)
    helper = load("tusk-hooks")
    user = tmp_path / ".git/hooks/commit-msg"
    user.write_text("#!/bin/sh\nexit 17\n")
    user.chmod(0o644)
    helper.reconcile(tmp_path, script_dir, ROOT / "hooks/git", "source")
    assert "version-bump-check" in (tmp_path / ".git/hooks/pre-push").read_text()
    assert not os.access(str(user) + ".pre-tusk", os.X_OK)


def test_same_version_upgrade_repairs_hooks(tmp_path, monkeypatch):
    init(tmp_path)
    script_dir = install(tmp_path)
    (tmp_path / ".git/hooks/pre-commit").unlink()
    upgrade = load("tusk-upgrade")
    version = int((ROOT / "VERSION").read_text())
    monkeypatch.setattr(upgrade, "get_latest_tag", lambda: f"v{version}")
    monkeypatch.setattr(upgrade, "get_remote_version", lambda _: version)
    # Exercise real main's same-version branches using an owned local release copy.
    import shutil
    release = tmp_path / "download/tusk-release"
    def ignore(directory, names):
        ignored = set(shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache")(directory, names))
        if Path(directory) == ROOT:
            ignored.add("tusk")
        return ignored
    shutil.copytree(ROOT, release, ignore=ignore, symlinks=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(ROOT / "bin/tusk-upgrade.py"), str(tmp_path), str(script_dir), "--no-commit", "--_rexec-src", str(release)])
    upgrade.main()
    assert (tmp_path / ".git/hooks/pre-commit").is_file()
