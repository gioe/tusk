"""Regression test for issue #882.

bin/tusk-generate-manifest.py's ``get_repo_root()`` previously invoked
``git rev-parse --show-toplevel`` against ``$PWD``. When the script was
invoked by absolute path from a sibling repo's CWD — e.g.
``/path/to/worktree/bin/tusk generate-manifest`` run from primary's CWD —
git walked up from PWD and returned primary's root. The on-disk walk then
enumerated primary's ``bin/`` instead of the worktree's, silently producing
a stale MANIFEST that omitted any new ``bin/tusk-*.py`` file the operator
just added to the worktree. The user-facing output was indistinguishable
from a clean run.

TASK-516 changes ``get_repo_root()`` to derive the root from
``__file__`` — invariant against the caller's CWD. This test pins that
invariant by standing up two real source-repo-shaped layouts (primary +
worktree, each with their own ``bin/tusk`` marker file and their own
``bin/tusk-*.py`` set) and confirming the worktree's script enumerates
the worktree's bin even when invoked from primary's CWD via absolute
path. The source-repo guard's continued correctness is also pinned: when
the script lives in a tempdir without a sibling ``bin/tusk``, the guard
in ``main()`` still fires.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-generate-manifest.py")
SOURCE_UNDERSCORE_HELPER = os.path.join(
    REPO_ROOT, "bin", "tusk_underscore_bin_files.py",
)
SOURCE_DIST_EXCLUDED = os.path.join(REPO_ROOT, "bin", "dist-excluded.txt")


def _build_source_repo_layout(root, extra_bin_scripts=()):
    """Build a minimal source-repo-shaped tree at ``root``.

    Includes: bin/tusk (so the source-repo guard in main() passes),
    bin/tusk-generate-manifest.py (the target under test, copied from the
    real source), the underscore helper module + dist-excluded.txt that
    the target imports, and any caller-supplied extra ``bin/tusk-*.py``
    scripts. Returns the path to ``bin/tusk-generate-manifest.py`` inside
    the layout — invoke this path to exercise the __file__-based root
    resolution.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # The presence of bin/tusk is what main()'s source-repo guard checks.
    (bin_dir / "tusk").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "tusk").chmod(0o755)
    shutil.copy(SOURCE_SCRIPT, bin_dir / "tusk-generate-manifest.py")
    shutil.copy(SOURCE_UNDERSCORE_HELPER, bin_dir / "tusk_underscore_bin_files.py")
    shutil.copy(SOURCE_DIST_EXCLUDED, bin_dir / "dist-excluded.txt")
    # .claude/ must exist so the .claude/tusk-manifest.json sibling write
    # at the end of main() doesn't FileNotFoundError.
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    for fname, content in extra_bin_scripts:
        (bin_dir / fname).write_text(content, encoding="utf-8")
    return bin_dir / "tusk-generate-manifest.py"


def _run_generate(script_path, cwd):
    env = os.environ.copy()
    return subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_worktree_bin_enumerated_when_invoked_from_primary_cwd(tmp_path):
    """Core issue #882 pin: invoking the worktree's script from primary's
    CWD must enumerate the WORKTREE'S bin/, not primary's.

    Layout:
      primary/  — bin/tusk-foo.py (only)
      worktree/ — bin/tusk-foo.py + bin/tusk-NEW.py

    With the old PWD-based ``git rev-parse`` resolution, invoking
    worktree/bin/tusk-generate-manifest.py from primary's CWD would have
    enumerated primary/bin/ (only tusk-foo) and written
    worktree/MANIFEST omitting tusk-NEW. The new ``__file__``-based
    resolution makes the worktree the resolved root regardless of CWD.
    """
    primary = tmp_path / "primary"
    worktree = tmp_path / "worktree"

    # Both layouts get the shared tusk-foo.py; only the worktree gets the
    # NEW script — that is the difference the test detects.
    _build_source_repo_layout(
        primary,
        extra_bin_scripts=[("tusk-foo.py", "# primary's foo\n")],
    )
    worktree_script = _build_source_repo_layout(
        worktree,
        extra_bin_scripts=[
            ("tusk-foo.py", "# worktree's foo\n"),
            ("tusk-NEW.py", "# worktree-only new script\n"),
        ],
    )

    # Invoke worktree's generate-manifest from primary's CWD via absolute
    # path — this is the exact failure mode the reporter described.
    result = _run_generate(worktree_script, cwd=primary)

    assert result.returncode == 0, (
        f"generate-manifest exited non-zero from primary CWD:\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # The MANIFEST must land at WORKTREE's root, not primary's.
    primary_manifest = primary / "MANIFEST"
    worktree_manifest = worktree / "MANIFEST"
    assert worktree_manifest.exists(), (
        f"Worktree's MANIFEST not written. stdout: {result.stdout!r}"
    )
    assert not primary_manifest.exists(), (
        "Primary's MANIFEST should not have been touched — the script ran "
        "with worktree as the resolved root."
    )

    entries = json.loads(worktree_manifest.read_text(encoding="utf-8"))
    assert ".claude/bin/tusk-NEW.py" in entries, (
        f"Worktree-only tusk-NEW.py not in MANIFEST. Entries: {entries!r}"
    )
    assert ".claude/bin/tusk-foo.py" in entries
    # Negative pin: primary's bin/ was NOT walked, so its content cannot
    # have leaked into worktree's MANIFEST. (Both happen to have tusk-foo
    # so we can't assert on that, but tusk-NEW only existing on worktree
    # is the discriminator above.)


def test_no_dependency_on_pwd_being_a_git_repo(tmp_path):
    """``get_repo_root()`` no longer shells out to git, so the script
    must succeed even when CWD is not inside any git repo at all.

    Sets CWD to a tmpdir with no ``.git`` directory anywhere in its
    parent chain (modulo the host repo's own .git, which doesn't matter
    because the new resolver doesn't consult git).
    """
    root = tmp_path / "src-repo"
    script = _build_source_repo_layout(
        root,
        extra_bin_scripts=[("tusk-helper.py", "# helper\n")],
    )

    # CWD: a sibling tmpdir that intentionally has nothing git-related.
    bare_cwd = tmp_path / "no-git-here"
    bare_cwd.mkdir()
    result = _run_generate(script, cwd=bare_cwd)

    assert result.returncode == 0, (
        f"Expected success even from non-git CWD; got\nstdout: "
        f"{result.stdout!r}\nstderr: {result.stderr!r}"
    )
    manifest = root / "MANIFEST"
    assert manifest.exists()
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    assert ".claude/bin/tusk-helper.py" in entries


def test_source_repo_guard_still_fires_outside_source_repo(tmp_path):
    """The source-repo guard in ``main()`` refuses when the resolved root
    lacks ``bin/tusk``. The fix's __file__-based resolution must not
    accidentally bypass that guard.

    Layout: a tempdir with ``bin/tusk-generate-manifest.py`` but NO
    ``bin/tusk`` — i.e. the script is somewhere that is not a source-repo
    layout. The guard should fire with the documented error message.
    """
    rogue = tmp_path / "rogue"
    bin_dir = rogue / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(SOURCE_SCRIPT, bin_dir / "tusk-generate-manifest.py")
    shutil.copy(SOURCE_UNDERSCORE_HELPER, bin_dir / "tusk_underscore_bin_files.py")
    shutil.copy(SOURCE_DIST_EXCLUDED, bin_dir / "dist-excluded.txt")
    # Deliberately NO bin/tusk file.

    script = bin_dir / "tusk-generate-manifest.py"
    result = _run_generate(script, cwd=rogue)

    assert result.returncode != 0, (
        f"Expected source-repo guard to refuse; got rc=0\nstdout: {result.stdout!r}"
    )
    assert "must be run inside the tusk source repo" in result.stderr, (
        f"Expected source-repo guard message; got: {result.stderr!r}"
    )


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, encoding="utf-8",
    )


def _git_init_commit(root):
    """git init + commit everything under ``root`` so git ls-files sees it."""
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "layout"], root)


def test_sparse_checkout_enumerates_via_git_ls_files(tmp_path):
    """Issue #1125: generate-manifest no longer REFUSES under sparse-checkout —
    it enumerates the complete tracked-file set from ``git ls-files`` (which
    reads the index and is complete even under sparse) instead of the partial
    on-disk walk. This pins the new behaviour: under sparse-checkout the command
    succeeds and the committed bin script is present in MANIFEST, even when CWD
    is a different (non-sparse) repo (the __file__-derived root still governs).
    """
    root = tmp_path / "src-repo"
    script = _build_source_repo_layout(
        root,
        extra_bin_scripts=[("tusk-helper.py", "# helper\n")],
    )
    _git_init_commit(root)
    # Enable sparse-checkout so _sparse_checkout_active(root) returns True.
    _git(["config", "core.sparseCheckout", "true"], root)

    # CWD: a sibling non-sparse repo — the fix must read sparse state and the
    # tracked list against the __file__-derived root, NOT CWD.
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    _git(["init", "-q", "-b", "main"], sibling)

    result = _run_generate(script, cwd=sibling)
    assert result.returncode == 0, (
        f"Expected success under sparse-checkout (issue #1125); got rc="
        f"{result.returncode}\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    entries = json.loads((root / "MANIFEST").read_text(encoding="utf-8"))
    assert ".claude/bin/tusk-helper.py" in entries, (
        f"Committed bin script missing from MANIFEST under sparse-checkout. "
        f"Entries: {entries!r}"
    )


def test_sparse_vs_full_manifest_parity(tmp_path):
    """Issue #1125 core guard: the MANIFEST produced under a sparse worktree
    (with out-of-cone files unmaterialized on disk) is byte-identical to the one
    produced from a full checkout of the same commit. Proves the git-ls-files
    enumeration recovers entries the on-disk walk would silently drop.
    """
    root = tmp_path / "src-repo"
    _build_source_repo_layout(
        root,
        extra_bin_scripts=[("tusk-foo.py", "# foo\n")],
    )
    script = root / "bin" / "tusk-generate-manifest.py"

    # Populate categories that will be unmaterialized by a bin/.claude-only cone.
    (root / "skills" / "myskill").mkdir(parents=True)
    (root / "skills" / "myskill" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (root / "codex-prompts").mkdir(parents=True)
    (root / "codex-prompts" / "foo.md").write_text("# prompt\n", encoding="utf-8")

    _git_init_commit(root)

    # Full checkout (non-sparse) → canonical MANIFEST via the on-disk walk.
    result_full = _run_generate(script, cwd=root)
    assert result_full.returncode == 0, result_full.stderr
    manifest_full = (root / "MANIFEST").read_text(encoding="utf-8")
    # Sanity: the out-of-cone entries are present in the full manifest.
    full_entries = json.loads(manifest_full)
    assert ".claude/skills/myskill/SKILL.md" in full_entries
    assert ".codex/prompts/foo.md" in full_entries

    # Restrict the cone to bin + .claude (the latter so the tusk-manifest.json
    # write target stays materialized); skills/ and codex-prompts/ drop off disk.
    _git(["sparse-checkout", "init", "--cone"], root)
    _git(["sparse-checkout", "set", "bin", ".claude"], root)
    assert not (root / "skills" / "myskill" / "SKILL.md").exists(), (
        "Precondition failed: skill file should be unmaterialized under the cone"
    )
    assert not (root / "codex-prompts" / "foo.md").exists(), (
        "Precondition failed: codex prompt should be unmaterialized under the cone"
    )

    # Sparse checkout → MANIFEST via git ls-files. Must equal the full manifest.
    result_sparse = _run_generate(script, cwd=root)
    assert result_sparse.returncode == 0, (
        f"Expected success under sparse cone; got rc={result_sparse.returncode}\n"
        f"stderr: {result_sparse.stderr!r}"
    )
    manifest_sparse = (root / "MANIFEST").read_text(encoding="utf-8")

    assert manifest_sparse == manifest_full, (
        "Sparse MANIFEST differs from full MANIFEST — the git-ls-files "
        "enumeration must recover out-of-cone entries byte-identically.\n"
        f"full:   {manifest_full!r}\nsparse: {manifest_sparse!r}"
    )


def test_unit_get_repo_root_returns_script_grandparent():
    """Unit-shape pin on the new resolver: invoking ``get_repo_root()``
    from the actual installed script returns the directory two levels up
    from the script (i.e. the repo root containing ``bin/``)."""
    spec = importlib.util.spec_from_file_location(
        "tusk_generate_manifest_under_test", SOURCE_SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = os.path.dirname(os.path.dirname(os.path.abspath(SOURCE_SCRIPT)))
    assert mod.get_repo_root() == expected


def test_unit_get_repo_root_is_cwd_independent(tmp_path, monkeypatch):
    """Unit-shape pin on the new resolver: changing CWD does not change
    the resolved root. Pre-fix this test would have failed because git
    rev-parse follows the CWD."""
    spec = importlib.util.spec_from_file_location(
        "tusk_generate_manifest_under_test_2", SOURCE_SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = os.path.dirname(os.path.dirname(os.path.abspath(SOURCE_SCRIPT)))

    before = mod.get_repo_root()
    monkeypatch.chdir(tmp_path)
    after = mod.get_repo_root()
    assert before == after == expected
