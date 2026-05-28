"""Integration tests for the PATH-resolved-tusk worktree advisory (issue #941).

When ``tusk`` is invoked via PATH from inside a checkout whose own ``bin/tusk``
is a *different file*, the SCRIPT_DIR-relative Python-helper dispatch loads
the invoked checkout's helpers, silently ignoring the CWD-local edits. The
CLI still exits 0 with stale-but-plausible output, masquerading as a passing
live check (original incident TASK-517: ``tusk review validate-comments 435``
from a worktree returned JSON missing the new ``dismissed_general`` key the
worktree had just added).

``maybe_warn_path_resolved_tusk_overrides_cwd`` in ``bin/tusk`` surfaces this
silently-stale-output gotcha as a one-line stderr advisory naming both paths.
Gated identically to the sibling source-repo / cross-repo drift advisories:
TTY-only by default, suppressed by ``TUSK_QUIET=1`` or the dedicated
``TUSK_NO_WORKTREE_BIN_ADVISORY=1``, restorable in non-TTY contexts by
``TUSK_FORCE_WARN=1``.

These tests stand up two real sibling checkouts (each with its own
``bin/tusk``) and invoke the "primary" binary from each CWD under each
gating condition. They use ``tusk version`` because it neither needs the DB
nor a configured git repo, so the warning gate is the only behavior under
test.
"""

import os
import shutil
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN_SOURCE = os.path.join(REPO_ROOT, "bin", "tusk")
TUSK_LOADER_SOURCE = os.path.join(REPO_ROOT, "bin", "tusk_loader.py")
VERSION_SOURCE = os.path.join(REPO_ROOT, "VERSION")
CONFIG_SOURCE = os.path.join(REPO_ROOT, "config.default.json")

WARNING_FRAGMENT = "use ./bin/tusk to test worktree-local edits"


def _seed_checkout(path):
    """Copy bin/tusk + the minimum support files into ``path``.

    Returns the absolute path to the new ``bin/tusk``. No git is initialized
    — these tests don't drive any subcommand that needs a repo.
    """
    bin_dir = path / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(TUSK_BIN_SOURCE, bin_dir / "tusk")
    os.chmod(bin_dir / "tusk", 0o755)
    if os.path.exists(TUSK_LOADER_SOURCE):
        shutil.copy(TUSK_LOADER_SOURCE, bin_dir / "tusk_loader.py")
    shutil.copy(VERSION_SOURCE, path / "VERSION")
    shutil.copy(CONFIG_SOURCE, path / "config.default.json")
    return bin_dir / "tusk"


def _run(binary, cwd, extra_env=None):
    """Invoke ``binary version`` from ``cwd`` with stderr captured.

    Hard-pins TUSK_DB to a throwaway path and isolates the active-projects
    registry so the cross-repo drift warning never fires and pollutes stderr.
    Default-includes TUSK_FORCE_WARN=1 so the advisory's TTY gate doesn't
    suppress it under pytest's captured stderr.
    """
    env = os.environ.copy()
    env["TUSK_DB"] = "/tmp/tusk-path-resolved-advisory-tests-no-db.db"
    env["TUSK_STATE_DIR"] = str(cwd.parent / "tusk-state")
    env["TUSK_FORCE_WARN"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(binary), "version"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_warning_fires_when_path_tusk_differs_from_cwd_bin_tusk(tmp_path):
    """The bug case: primary's bin/tusk invoked from a sibling cwd that
    has its own different bin/tusk → warning surfaces both paths."""
    primary = tmp_path / "primary"
    sibling = tmp_path / "sibling"
    primary_bin = _seed_checkout(primary)
    _seed_checkout(sibling)

    result = _run(primary_bin, cwd=sibling)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT in result.stderr
    # Both paths named — invoked + cwd.
    assert str(primary_bin) in result.stderr
    # The cwd path is resolved via cd -P, so macOS /tmp may surface as
    # /private/tmp. Match on the trailing bin/tusk fragment to avoid a
    # platform-specific assertion.
    assert "/bin/tusk also exists" in result.stderr


def test_warning_suppressed_when_tusk_no_worktree_bin_advisory_set(tmp_path):
    """TUSK_NO_WORKTREE_BIN_ADVISORY=1 silences the warning in the otherwise-fire case."""
    primary = tmp_path / "primary"
    sibling = tmp_path / "sibling"
    primary_bin = _seed_checkout(primary)
    _seed_checkout(sibling)

    result = _run(
        primary_bin,
        cwd=sibling,
        extra_env={"TUSK_NO_WORKTREE_BIN_ADVISORY": "1"},
    )

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_suppressed_when_tusk_quiet_set(tmp_path):
    """TUSK_QUIET=1 suppresses every tusk warning, including this one."""
    primary = tmp_path / "primary"
    sibling = tmp_path / "sibling"
    primary_bin = _seed_checkout(primary)
    _seed_checkout(sibling)

    result = _run(primary_bin, cwd=sibling, extra_env={"TUSK_QUIET": "1"})

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_suppressed_when_cwd_bin_tusk_is_same_file(tmp_path):
    """Criterion 4: invoked from the primary checkout's own dir — paths match,
    no advisory."""
    primary = tmp_path / "primary"
    primary_bin = _seed_checkout(primary)

    result = _run(primary_bin, cwd=primary)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_suppressed_when_no_bin_tusk_in_cwd(tmp_path):
    """Criterion 5: arbitrary cwd with no bin/tusk (consumer project, $HOME,
    /tmp) — no advisory."""
    primary = tmp_path / "primary"
    arbitrary = tmp_path / "arbitrary"
    arbitrary.mkdir()
    primary_bin = _seed_checkout(primary)

    result = _run(primary_bin, cwd=arbitrary)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT not in result.stderr


def test_warning_suppressed_in_non_tty_without_force_warn(tmp_path):
    """Without TUSK_FORCE_WARN=1 and stderr captured (non-TTY), the advisory
    stays silent — same default-quiet behavior as the sibling drift warnings."""
    primary = tmp_path / "primary"
    sibling = tmp_path / "sibling"
    primary_bin = _seed_checkout(primary)
    _seed_checkout(sibling)

    env = os.environ.copy()
    env["TUSK_DB"] = "/tmp/tusk-path-resolved-advisory-tests-no-db.db"
    env["TUSK_STATE_DIR"] = str(tmp_path / "tusk-state")
    env.pop("TUSK_FORCE_WARN", None)
    result = subprocess.run(
        [str(primary_bin), "version"],
        cwd=str(sibling),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert WARNING_FRAGMENT not in result.stderr
