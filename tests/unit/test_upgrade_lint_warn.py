"""Unit tests for tusk-upgrade.py copy_bin_files lint-modification warning.

Verifies that tusk upgrade warns when .claude/bin/tusk-lint.py has local
modifications that would be overwritten, and stays silent when the file is
unchanged. Covers GitHub Issue #370 where custom lint functions were silently
destroyed by upgrade.
"""

import hashlib
import importlib.util
import os
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPGRADE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-upgrade.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_upgrade", UPGRADE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCopyBinFilesLintWarning:
    def _make_bin_src(self, tmp_path, lint_content: str) -> Path:
        """Create a minimal src/bin/ with a tusk-lint.py."""
        src_bin = tmp_path / "src" / "bin"
        src_bin.mkdir(parents=True)
        (src_bin / "tusk").write_text("#!/bin/bash\n")
        (src_bin / "tusk").chmod(0o755)
        (src_bin / "tusk-lint.py").write_text(lint_content)
        (src_bin / "tusk_loader.py").write_text("# loader\n")
        (src_bin / "tusk_skill_filter.py").write_text("# skill filter\n")
        (tmp_path / "src" / "config.default.json").write_text("{}\n")
        (tmp_path / "src" / "pricing.json").write_text("{}\n")
        return tmp_path / "src"

    def test_warns_when_lint_py_locally_modified(self, tmp_path, capsys):
        """copy_bin_files prints a warning when installed tusk-lint.py has local modifications.

        The warning fires when the sidecar hash (what tusk last wrote) differs from the
        currently-installed file (meaning the user edited it since the last upgrade).
        """
        mod = _load_module()
        src = self._make_bin_src(tmp_path, "# new version\n")
        script_dir = tmp_path / "script_dir"
        script_dir.mkdir()
        # Simulate a locally-modified installed copy: user added a custom rule on top of
        # "# previously installed version\n" (which is what the sidecar records).
        previously_installed = b"# previously installed version\n"
        (script_dir / "tusk-lint.py").write_text("# local custom rule added\n")
        (script_dir / "tusk-lint.py.hash").write_text(
            hashlib.md5(previously_installed).hexdigest() + "\n"
        )

        mod.copy_bin_files(str(src), str(script_dir))

        captured = capsys.readouterr()
        assert "tusk-lint.py" in captured.out
        assert "overwritten" in captured.out
        assert "tusk-lint-extra.py" in captured.out

    def test_no_warning_when_lint_py_unchanged(self, tmp_path, capsys):
        """copy_bin_files does not warn when installed tusk-lint.py matches the sidecar hash.

        When the installed file is identical to what tusk last wrote (sidecar hash matches),
        no warning is printed — the file has not been locally modified.
        """
        mod = _load_module()
        src = self._make_bin_src(tmp_path, "# canonical content\n")
        script_dir = tmp_path / "script_dir"
        script_dir.mkdir()
        # Installed copy is identical to what tusk last wrote — sidecar hash matches.
        canonical = b"# canonical content\n"
        (script_dir / "tusk-lint.py").write_text(canonical.decode())
        (script_dir / "tusk-lint.py.hash").write_text(
            hashlib.md5(canonical).hexdigest() + "\n"
        )

        mod.copy_bin_files(str(src), str(script_dir))

        captured = capsys.readouterr()
        assert "overwritten" not in captured.out

    def test_no_warning_when_lint_py_not_yet_installed(self, tmp_path, capsys):
        """copy_bin_files does not warn when tusk-lint.py does not yet exist (first install)."""
        mod = _load_module()
        src = self._make_bin_src(tmp_path, "# new content\n")
        script_dir = tmp_path / "script_dir"
        script_dir.mkdir()
        # No pre-existing tusk-lint.py

        mod.copy_bin_files(str(src), str(script_dir))

        captured = capsys.readouterr()
        assert "overwritten" not in captured.out
