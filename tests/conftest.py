"""Shared pytest fixtures for tusk tests.

All Python scripts under bin/ accept:
    sys.argv[1] — db_path
    sys.argv[2] — config_path
    sys.argv[3:] — command-specific flags

Use these fixtures as the foundation for all unit and integration tests.
"""

import os
import subprocess

import pytest

# Repo root is the parent of the tests/ directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

# SQLite note: DB trigger and CHECK violations raise sqlite3.IntegrityError,
# NOT sqlite3.OperationalError. Use IntegrityError in pytest.raises() for trigger tests.


@pytest.fixture(autouse=True)
def _clear_tusk_env(monkeypatch):
    """Strip tusk env-var pinning from each test's environment.

    `bin/tusk` exports TUSK_REPO_ROOT (and TUSK_PROJECT when set) so child
    Python scripts can resolve the project DB even when invoked from a linked
    worktree. When `tusk test-precheck` invokes pytest, those env vars leak
    into the test process — tests that import a script via importlib and pass
    a fake `repo_root` (e.g. test_commit_output_capture_issue450.py) then see
    `_resolve_db_path` ignore their argv-passed root and reach for the real
    project DB instead, producing rc-mismatch failures only under
    test-precheck. Clear these vars so unit tests stay hermetic; the
    `db_path` fixture re-pins TUSK_DB for tests that need a real DB.
    """
    monkeypatch.delenv("TUSK_REPO_ROOT", raising=False)
    monkeypatch.delenv("TUSK_PROJECT", raising=False)
    monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Restore the process working directory after every test (issue #1084).

    Several integration tests import ``bin/tusk-merge.py`` and call its
    functions in-process. The merge cleanup path calls ``os.chdir`` to
    relocate out of a worktree it is about to remove
    (``_remove_recorded_task_worktree``), and the chdir-into-recorded-workspace
    gate (issue #764) does the same. Those tests install an ``os.chdir`` spy
    that calls the *real* ``os.chdir``, but ``monkeypatch.setattr`` only
    restores the patched attribute — never the working directory. The pytest
    process is therefore left inside a ``tmp_path`` workspace that pytest then
    deletes during teardown, and every later test that resolves a repo root
    from ``os.getcwd()`` (scope checks, ``task-insert`` spec validation, etc.)
    fails with ``path does not exist at repo root`` — a deterministic,
    order-dependent failure set of 34 tests in full runs.

    Snapshotting the CWD before each test and restoring it afterward makes
    that leak structurally impossible regardless of which test misbehaves.
    ``monkeypatch.chdir`` callers already self-restore; this is the safety net
    for the in-process production ``os.chdir`` that no fixture owns.
    """
    try:
        original = os.getcwd()
    except OSError:
        original = REPO_ROOT
    try:
        yield
    finally:
        try:
            os.chdir(original)
        except OSError:
            # The original directory was removed mid-test; fall back to the
            # repo root so subsequent tests still resolve paths sanely.
            os.chdir(REPO_ROOT)


@pytest.fixture(autouse=True)
def _isolate_tusk_state_dir(tmp_path_factory, monkeypatch):
    """Pin TUSK_STATE_DIR to a per-test temp dir (issue #1084).

    ``bin/tusk`` keeps its cross-repo active-projects registry under
    ``$TUSK_STATE_DIR`` (default ``$HOME/.tusk``). Integration tests that run
    ``tusk task-start`` / ``session-close`` as subprocesses otherwise read and
    write the developer's real ``~/.tusk/active-projects`` file — shared
    mutable state that both pollutes the real environment and lets tests
    observe each other's registrations across a full run. Each test gets its
    own throwaway state dir; tests that need a specific value still override
    via their own ``monkeypatch.setenv`` (which wins, since it runs after this
    autouse fixture).
    """
    state_dir = tmp_path_factory.mktemp("tusk-state")
    monkeypatch.setenv("TUSK_STATE_DIR", str(state_dir))


@pytest.fixture()
def config_path():
    """Return the path to config.default.json."""
    return os.path.join(REPO_ROOT, "config.default.json")


@pytest.fixture()
def db_path(tmp_path, config_path, monkeypatch):
    """Initialise a fresh tusk SQLite DB in tmp_path via `bin/tusk init`.

    Pins TUSK_DB in the test's environment via monkeypatch so that *any*
    subprocess a test (or its code-under-test) spawns inherits the override
    and hits this isolated DB. Without this, helpers like tusk-abandon and
    tusk-merge — which shell out to `tusk session-close` / `tusk task-done`
    — silently hit the repo's live tusk/tasks.db and produce opaque
    "Session N already closed" / "Task N already Done" failures that
    reference IDs from the live DB. See TASK-53.

    Path-layout gotcha: the DB sits at ``tmp_path/tasks.db`` (flat), not at
    ``tmp_path/tusk/tasks.db`` (production layout). Any code-under-test that
    resolves a repo-rooted path via ``dirname(dirname(db_path))/<subpath>``
    (e.g. migration 29 looking for ``tusk/conventions.md``, migration 47
    looking for ``docs/PILLARS.md``) will land at the *parent* of
    ``tmp_path``, not inside it. For those tests, build a nested fixture —
    create ``tmp_path/tusk/tasks.db`` with ``TUSK_DB`` pinned to that path
    and place the sibling files under ``tmp_path/...`` — so that
    ``dirname(dirname(db))`` resolves to ``tmp_path``. See
    ``tests/integration/test_migrate_47.py::repo_with_pillars_md`` for the
    canonical pattern.
    """
    db_file = tmp_path / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_file))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"tusk init failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert db_file.exists(), f"Expected DB at {db_file} after tusk init"
    return db_file
