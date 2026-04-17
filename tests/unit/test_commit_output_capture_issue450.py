"""Unit tests for tusk commit output-capture behavior (GitHub Issue #450).

Covers the quiet-by-default output contract:
  - test_command stdout/stderr is captured (not streamed) unless --verbose is set
  - On test failure, the captured output is dumped before the error message
  - On test success, a brief "tests passed (Xs)" marker is emitted
  - The final line of stdout is always TUSK_COMMIT_RESULT: {...} for every exit path
  - --verbose restores streaming behavior (capture_output=False)
"""

import importlib.util
import json
import os
import re
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


def _make_repo(tmp_path):
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


def _parse_summary(stdout: str) -> dict:
    """Locate the TUSK_COMMIT_RESULT line and return its decoded payload."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, "stdout was empty — expected at least a summary line"
    last = lines[-1]
    assert last.startswith("TUSK_COMMIT_RESULT: "), (
        f"final line must be the summary; got {last!r}"
    )
    return json.loads(last[len("TUSK_COMMIT_RESULT: "):])


class TestSummaryLineEmission:
    """The summary line must be the last line of stdout for every exit path."""

    def test_summary_on_test_success(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "echo pytest-noise"})

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "echo pytest-noise":
                return subprocess.CompletedProcess(args, 0, stdout="noise\n", stderr="")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        out = capsys.readouterr().out

        assert rc == 0
        payload = _parse_summary(out)
        assert payload["status"] == "success"
        assert payload["exit_code"] == 0
        assert payload["task"] == 999
        assert payload["commit"] and re.fullmatch(r"[0-9a-f]{12}", payload["commit"])

    def test_summary_on_test_failure(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "false"})

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "false":
                return subprocess.CompletedProcess(
                    args, 1, stdout="FAILED test_x\n", stderr="assertion error\n"
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        captured = capsys.readouterr()

        assert rc == 2
        payload = _parse_summary(captured.out)
        assert payload["status"] == "failure"
        assert payload["exit_code"] == 2
        assert payload["task"] == 999
        assert payload["commit"] is None

    def test_summary_on_timeout(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "sleep 30",
            "test_command_timeout_sec": 1,
        })

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "sleep 30":
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        out = capsys.readouterr().out

        assert rc == 5
        payload = _parse_summary(out)
        assert payload["status"] == "failure"
        assert payload["exit_code"] == 5
        assert payload["commit"] is None

    def test_summary_on_criterion_failure_includes_sha(self, tmp_path, monkeypatch, capsys):
        """Criterion-done failure (exit 4) still reports the commit SHA since the commit landed."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "true"})

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            # Simulate lint passing.
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # Simulate the test_command passing.
            if kw.get("shell") and args == "true":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            # Simulate "tusk criteria done" failing so we exit 4 after the
            # commit has already landed.
            if isinstance(args, list) and len(args) >= 3 and args[-3:-1] == ["criteria", "done"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target), "--criteria", "42"]
        rc = mod.main(argv)
        out = capsys.readouterr().out

        assert rc == 4
        payload = _parse_summary(out)
        assert payload["status"] == "failure"
        assert payload["exit_code"] == 4
        # The commit DID land before the criteria step failed — the summary
        # must still report the SHA so callers can recover the commit.
        assert payload["commit"], (
            "criterion-failure path must preserve the landed commit SHA"
        )
        assert re.fullmatch(r"[0-9a-f]{12}", payload["commit"])

    def test_summary_on_argv_error(self, tmp_path, capsys):
        """Even an early-exit usage error must still print the summary line."""
        mod = _load_module()
        # argv too short to parse — should trigger the usage error path.
        rc = mod.main([str(tmp_path), "/nonexistent/config.json"])
        out = capsys.readouterr().out

        assert rc == 1
        payload = _parse_summary(out)
        assert payload["status"] == "failure"
        assert payload["exit_code"] == 1
        # task_id was never parsed, so it remains null in the summary.
        assert payload["task"] is None
        assert payload["commit"] is None


class TestCapturedOutputBehavior:
    """Quiet-by-default capture: streams only on --verbose; dumps on failure."""

    def test_test_command_captured_by_default(self, tmp_path, monkeypatch, capsys):
        """Without --verbose, subprocess.run for test_command must pass capture_output=True."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "echo noise"})

        observed = {}
        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "echo noise":
                observed["capture_output"] = kw.get("capture_output")
                return subprocess.CompletedProcess(args, 0, stdout="noise\n", stderr="")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)

        assert rc == 0
        assert observed["capture_output"] is True, (
            "Default mode must capture test output to keep stdout short"
        )

    def test_verbose_flag_streams_output(self, tmp_path, monkeypatch, capsys):
        """With --verbose, subprocess.run for test_command must pass capture_output=False."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "echo noise"})

        observed = {}
        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "echo noise":
                observed["capture_output"] = kw.get("capture_output")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target), "--verbose"]
        rc = mod.main(argv)

        assert rc == 0
        assert observed["capture_output"] is False, (
            "--verbose must stream test output (capture_output=False)"
        )

    def test_captured_output_dumped_on_failure(self, tmp_path, monkeypatch, capsys):
        """When tests fail in quiet mode, the captured stdout is surfaced so the failure is diagnosable."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "fake_pytest"})

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "fake_pytest":
                return subprocess.CompletedProcess(
                    args, 1,
                    stdout="FAILED tests/test_broken.py::test_it\n",
                    stderr="AssertionError: 1 != 2\n",
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        captured = capsys.readouterr()

        assert rc == 2
        # Captured stdout from the failing test must be surfaced to the user.
        assert "FAILED tests/test_broken.py::test_it" in captured.out
        # Captured stderr from the failing test must also be surfaced.
        assert "AssertionError: 1 != 2" in captured.err

    def test_timeout_dumps_partial_output(self, tmp_path, monkeypatch, capsys):
        """When test_command times out in quiet mode, partial stdout/stderr from the
        dying child must be surfaced so the user can see which test was hung."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {
            "test_command": "hang",
            "test_command_timeout_sec": 1,
        })

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "hang":
                raise subprocess.TimeoutExpired(
                    cmd=args,
                    timeout=1,
                    output="test_slow_thing started...\n",
                    stderr="warning from pytest\n",
                )
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        captured = capsys.readouterr()

        assert rc == 5
        assert "test_slow_thing started" in captured.out, (
            "partial child stdout must be surfaced on timeout"
        )
        assert "warning from pytest" in captured.err, (
            "partial child stderr must be surfaced on timeout"
        )

    def test_success_emits_brief_status_line(self, tmp_path, monkeypatch, capsys):
        """On test success, a one-line 'tests passed (Xs)' marker appears before the summary."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "true"})

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "true":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        out = capsys.readouterr().out

        assert rc == 0
        assert re.search(r"tests passed \(\d+\.\d+s\)", out), (
            f"expected brief 'tests passed (Xs)' line; got:\n{out}"
        )

    def test_quiet_mode_no_test_header_banner(self, tmp_path, monkeypatch, capsys):
        """In quiet mode the '=== Running test_command: ===' banner is suppressed."""
        mod = _load_module()
        repo, target = _make_repo(tmp_path)
        cfg = _write_config(tmp_path, {"test_command": "echo x"})

        real_run = subprocess.run

        def fake_run(args, *a, **kw):
            if isinstance(args, list) and args and args[-1] == "lint":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if kw.get("shell") and args == "echo x":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_run(args, *a, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.delenv("TUSK_TEST_COMMAND_TIMEOUT", raising=False)

        argv = [str(repo), cfg, "999", "msg", str(target)]
        rc = mod.main(argv)
        out = capsys.readouterr().out

        assert rc == 0
        assert "Running test_command" not in out, (
            "Quiet mode should not print the verbose test-command banner"
        )
