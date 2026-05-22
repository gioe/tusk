"""Unit tests for `tusk lint` timeout behavior in tusk-commit.py (Issue #795).

Companion to test_commit_timeout.py (which covers the test_command timeout).

Covers:
  - load_lint_timeout resolution (env var > config > default).
  - main() returns exit code 8 when the `tusk lint` subprocess raises
    subprocess.TimeoutExpired, with a stderr message that names the
    configured timeout and the source hint (env / config / default).
"""

import importlib.util
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


# ── Resolver: env > config > default ────────────────────────────────


class TestLoadLintTimeout:
    def test_default_when_nothing_set(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_LINT_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {})
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == mod.DEFAULT_LINT_TIMEOUT_SEC
        assert source == "default"

    def test_default_value_is_60_seconds(self):
        mod = _load_module()
        assert mod.DEFAULT_LINT_TIMEOUT_SEC == 60

    def test_config_value_used_when_env_unset(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_LINT_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"lint_timeout_sec": 90})
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == 90
        assert source == "config"

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_LINT_TIMEOUT", "15")
        cfg = _write_config(tmp_path, {"lint_timeout_sec": 90})
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == 15
        assert source == "env"

    def test_invalid_env_falls_through_to_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_LINT_TIMEOUT", "not-a-number")
        cfg = _write_config(tmp_path, {"lint_timeout_sec": 45})
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == 45
        assert source == "config"

    def test_non_positive_env_falls_through(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setenv("TUSK_LINT_TIMEOUT", "0")
        cfg = _write_config(tmp_path, {})
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == mod.DEFAULT_LINT_TIMEOUT_SEC
        assert source == "default"

    def test_invalid_config_falls_through_to_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_LINT_TIMEOUT", raising=False)
        cfg = _write_config(tmp_path, {"lint_timeout_sec": "garbage"})
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == mod.DEFAULT_LINT_TIMEOUT_SEC
        assert source == "default"

    def test_missing_config_file_returns_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.delenv("TUSK_LINT_TIMEOUT", raising=False)
        cfg = str(tmp_path / "nonexistent.json")
        timeout, source = mod.load_lint_timeout(cfg)
        assert timeout == mod.DEFAULT_LINT_TIMEOUT_SEC
        assert source == "default"


# ── main() returns exit 8 when lint times out ───────────────────────


class TestLintTimeoutPath:
    """Verify main() returns exit code 8 when the lint subprocess hits the timeout."""

    def _make_repo(self, tmp_path):
        """Create a throwaway git repo with one tracked file to commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        seed = repo / "seed.txt"
        seed.write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "seed.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
        target = repo / "file.txt"
        target.write_text("change\n")
        return repo, target

    def test_lint_timeout_returns_exit_8_with_message(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"lint_timeout_sec": 1})
        monkeypatch.delenv("TUSK_LINT_TIMEOUT", raising=False)

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            # Raise TimeoutExpired only on the lint invocation; let everything
            # else (git operations, etc.) go through real subprocess.run.
            if isinstance(args, list) and args and "lint" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert exit_code == 8, (
            f"expected exit 8 (lint timeout), got {exit_code}. "
            f"stdout={captured.out!r} stderr={captured.err!r}"
        )
        assert "lint timed out after 1s" in combined
        assert "lint_timeout_sec" in combined
        # And the bypass guidance is surfaced so operators have an out.
        assert "--skip-lint" in combined

    def test_lint_timeout_message_cites_env_source_when_env_set(
        self, tmp_path, monkeypatch, capsys
    ):
        mod = _load_module()
        repo, target = self._make_repo(tmp_path)
        cfg = _write_config(tmp_path, {})
        monkeypatch.setenv("TUSK_LINT_TIMEOUT", "1")

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and "lint" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        exit_code = mod.main(argv)
        captured = capsys.readouterr()
        combined = captured.out + captured.err

        assert exit_code == 8
        assert "TUSK_LINT_TIMEOUT" in combined
