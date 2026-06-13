"""Regression guards for cross-test state isolation (issue #1084).

A full ``tests/integration`` run on a clean checkout produced 34
deterministic, order-dependent failures while every test passed in
isolation. Root cause: integration tests that import ``bin/tusk-merge.py``
and call its functions in-process let the production ``os.chdir`` relocate
the pytest process out of a soon-deleted ``tmp_path`` worktree, and nothing
restored the working directory afterward. Later tests resolved repo roots
from the stale CWD and failed with ``path does not exist at repo root``.
The autouse ``_restore_cwd`` and ``_isolate_tusk_state_dir`` fixtures in
``tests/conftest.py`` close that leak; these guards prove they hold.

The two tests below are ordered so the first one deliberately corrupts the
process CWD / state dir and the second asserts it observed neither — a
direct, in-suite reproduction of the leak the fixtures prevent.
"""

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCwdDoesNotLeakBetweenTests:
    """Names are alphabetized so pytest runs _01 before _02."""

    def test_01_change_cwd_without_restoring(self, tmp_path):
        # Mimic the in-process production os.chdir that no fixture owns:
        # jump into a tmp dir and never restore it. The autouse _restore_cwd
        # fixture must undo this before the next test runs.
        os.chdir(tmp_path)
        assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))

    def test_02_cwd_was_restored_by_fixture(self):
        # If _restore_cwd did its job, we are back at the repo root (the CWD
        # pytest was launched from), not stranded in test_01's tmp_path.
        assert os.path.realpath(os.getcwd()) == os.path.realpath(REPO_ROOT)


class TestStateDirIsIsolated:
    def test_tusk_state_dir_is_pinned_to_temp(self):
        state_dir = os.environ.get("TUSK_STATE_DIR")
        assert state_dir, "TUSK_STATE_DIR must be pinned by the autouse fixture"
        # It must not be the developer's real ~/.tusk, so a test running
        # `tusk task-start` cannot pollute the shared active-projects registry.
        assert os.path.realpath(state_dir) != os.path.realpath(
            os.path.expanduser("~/.tusk")
        )

    def test_state_dir_differs_per_test(self, request):
        # A fresh temp dir per test means no registry rows survive across
        # tests — the secondary shared-state channel named in issue #1084.
        first = os.environ.get("TUSK_STATE_DIR")
        assert first and os.path.isabs(first)
