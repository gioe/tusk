"""Unit tests for test_command timeout behavior in tusk-commit.py (Issue #483).

Covers:
  - load_test_command_timeout resolution (env var > config > default)
  - Fallback behavior for invalid values (negative, zero, non-numeric, missing)
  - main() returns exit code 5 when the test_command subprocess raises
    subprocess.TimeoutExpired, with a message that names the configured timeout
    and the source hint (config key or env var).
"""

import importlib.util
import io
import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_config(tmp_path, data: dict) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return str(p)


class TestLoadTestCommandTimeout:
    def test_default_when_nothing_set(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command": "pytest"})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"

    def test_config_value_used_when_env_unset(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "30")
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 30
        assert source == "env"

    def test_invalid_env_falls_back_to_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "not-a-number")
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_zero_or_negative_env_falls_through(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "0")
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": 45})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_invalid_config_falls_back_to_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"test_command_timeout_sec": -5})
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"

    def test_missing_config_file_returns_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)
        cfg = str(tmp_path / "nonexistent.json")
        timeout, source = mod.load_test_command_timeout(cfg)
        assert timeout == mod.DEFAULT_TEST_COMMAND_TIMEOUT_SEC
        assert source == "default"


class TestTimeoutPath:
    """Verify main() returns exit code 5 when test_command hits the timeout."""

    def _make_repo(self, tmp_path):
        """Create a throwaway git repo with a single tracked file to commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@t"], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "t"], check=True
        )
        # Seed an initial commit so HEAD exists for the pre-commit SHA probe.
        seed = repo / "seed.txt"
        seed.write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True
        )
        target = repo / "file.txt"
        target.write_text("change\n")
        return repo, target

    def test_timeout_returns_exit_5_with_message(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        # Stub the lint subprocess and the criteria-done subprocess (no criteria
        # here, so only lint is invoked via tusk_bin).  The test_cmd call runs
        # through the real subprocess.run — which we monkeypatch to raise
        # TimeoutExpired so the test completes in milliseconds, not seconds.
        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        # Bypass task validation — the pre-flight is normally what enforces
        # that task_id exists in the DB; we set _skip_task_check via the
        # documented env toggle if available, otherwise run the full pipeline.
        # This test hits the DB via load_task_domain only when
        # domain_test_commands is set — our config doesn't set it, so the
        # DB round-trip is skipped.
        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()

        assert exit_code == 5, (
            f"expected exit 5 (timeout), got {exit_code}. "
            f"stdout={captured.out!r} stderr={captured.err!r}"
        )
        combined = captured.out + captured.err
        assert "timed out after 1s" in combined
        assert "test_command_timeout_sec" in combined

    def test_timeout_message_cites_env_source_when_env_set(
        self, tmp_path, monkeypatch, capsys
    ):
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "sleep 30"})
        monkeypatch.setenv("TUSK_TEST_COMMAND_TIMEOUT", "1")

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert exit_code == 5
        assert "TUSK_TEST_COMMAND_TIMEOUT" in combined

    def test_timeout_with_bytes_payload_does_not_crash_issue561(
        self, tmp_path, monkeypatch, capsys
    ):
        # Regression: subprocess.TimeoutExpired can carry raw bytes on
        # output/stderr even when subprocess.run was called with text=True
        # (the buffered payload is captured before decoding when the timeout
        # fires).  The dump path at tusk-commit.py:744 used to call
        # sys.stdout.write(exc.stdout) unconditionally, which crashes with
        # `TypeError: write() argument must be str, not bytes` on the bytes
        # branch.  This test pins the defensive decode in place.
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=1,
                    output=b"buffered stdout from hung test\n",
                    stderr=b"buffered stderr from hung test\n",
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert exit_code == 5, (
            f"expected exit 5 (timeout), got {exit_code}. "
            f"stdout={captured.out!r} stderr={captured.err!r}"
        )
        assert "TypeError" not in combined
        assert "buffered stdout from hung test" in captured.out
        assert "buffered stderr from hung test" in captured.err
        assert "timed out after 1s" in combined

    def test_timeout_with_str_payload_still_writes_unchanged(
        self, tmp_path, monkeypatch, capsys
    ):
        # Sibling case: when the buffered payload is already str (the common
        # path), the new defensive branch must pass it through verbatim.
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=1,
                    output="str stdout payload\n",
                    stderr="str stderr payload\n",
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()

        assert exit_code == 5
        assert "str stdout payload" in captured.out
        assert "str stderr payload" in captured.err
