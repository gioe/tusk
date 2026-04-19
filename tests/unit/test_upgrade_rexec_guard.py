"""Unit tests for the --_rexec-src path guard in tusk-upgrade.py.

The hidden --_rexec-src flag is set only by our own os.execv handoff and always
points at a subpath of tempfile.gettempdir(). main() validates the path before
any filesystem work so that the finally-block rmtree can never be steered at
real user data by accidental or malicious manual invocation.
"""

import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPGRADE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-upgrade.py")


def _run(rexec_src: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, UPGRADE_SCRIPT,
            "/tmp/fake-repo", "/tmp/fake-script-dir",
            "--_rexec-src", rexec_src,
        ],
        capture_output=True, text=True,
    )


class TestRexecSrcGuard:
    def test_rejects_path_outside_tempdir(self):
        result = _run("/Users/does-not-matter")
        assert result.returncode != 0
        assert "--_rexec-src must be a subpath of" in result.stderr

    def test_rejects_repo_root(self, tmp_path):
        # tmp_path from pytest is under tempdir on most systems, so use a
        # path we know is NOT under gettempdir.
        outside = os.path.abspath("/etc")
        result = _run(outside)
        assert result.returncode != 0
        assert "--_rexec-src must be a subpath of" in result.stderr

    def test_rejects_root(self):
        result = _run("/")
        assert result.returncode != 0
        assert "--_rexec-src must be a subpath of" in result.stderr

    def test_accepts_tempdir_subpath(self):
        # A path under gettempdir() passes the guard — the process will then
        # fail later (fake repo/script_dir don't exist) but NOT from the guard.
        src = os.path.join(tempfile.gettempdir(), "tusk-upgrade-XXXXXX", "tusk-v999")
        result = _run(src)
        # Stderr must not mention the guard rejection.
        assert "--_rexec-src must be a subpath of" not in result.stderr
