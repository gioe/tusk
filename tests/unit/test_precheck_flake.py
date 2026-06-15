"""Unit tests for test-precheck's flake-aware signal (issue #1076).

Drives ``_emit_verdict`` directly with synthetic ``flake_exits`` lists so the
flaky_suspect logic is exercised without depending on a genuinely flaky command,
plus an end-to-end CLI run with a marker-flipping script for the --flake-retries
path.
"""

import importlib.util
import json
import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
PRECHECK_PY = os.path.join(BIN, "tusk-test-precheck.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")

_spec = importlib.util.spec_from_file_location("tusk_test_precheck", PRECHECK_PY)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture()
def plain_repo(tmp_path):
    """A git repo with one commit and no origin remote (divergence is a no-op)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], str(tmp_path))
    _git(["config", "user.email", "t@t.t"], str(repo))
    _git(["config", "user.name", "t"], str(repo))
    (repo / "a.txt").write_text("a\n")
    _git(["add", "a.txt"], str(repo))
    _git(["commit", "-m", "A"], str(repo))
    return str(repo)


def test_mixed_results_flag_flaky_suspect(plain_repo, capsys):
    mod._emit_verdict(plain_repo, BIN, "cmd", 1, False, None, flake_exits=[1, 0, 1])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["flake_runs_total"] == 3
    assert out["flake_failures"] == 2
    assert out["flaky_suspect"] is True
    assert "suspected flake" in captured.err


def test_consistent_pass_is_not_flaky(plain_repo, capsys):
    mod._emit_verdict(plain_repo, BIN, "cmd", 0, False, None, flake_exits=[0, 0, 0])
    out = json.loads(capsys.readouterr().out)
    assert out["flake_runs_total"] == 3
    assert out["flake_failures"] == 0
    assert out["flaky_suspect"] is False


def test_consistent_fail_is_not_flaky(plain_repo, capsys):
    mod._emit_verdict(plain_repo, BIN, "cmd", 1, False, None, flake_exits=[1, 1])
    out = json.loads(capsys.readouterr().out)
    assert out["flake_runs_total"] == 2
    assert out["flake_failures"] == 2
    assert out["flaky_suspect"] is False


def test_default_off_omits_flake_keys(plain_repo, capsys):
    mod._emit_verdict(plain_repo, BIN, "cmd", 1, False, None, flake_exits=None)
    out = json.loads(capsys.readouterr().out)
    assert "flaky_suspect" not in out
    assert "flake_runs_total" not in out
    assert "flake_failures" not in out


def test_single_run_list_omits_flake_keys(plain_repo, capsys):
    # A degenerate one-element list cannot establish flakiness.
    mod._emit_verdict(plain_repo, BIN, "cmd", 1, False, None, flake_exits=[1])
    out = json.loads(capsys.readouterr().out)
    assert "flaky_suspect" not in out


# ---------------------------------------------------------------------------
# End-to-end: --flake-retries drives repeated HEAD runs against a flaky command
# ---------------------------------------------------------------------------


def _write_config(tmp_path, test_command) -> str:
    with open(CONFIG_DEFAULT, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["test_command"] = test_command
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def _run_precheck(repo, config_path, *extra):
    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    return subprocess.run(
        ["python3", PRECHECK_PY, repo, config_path, *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=repo, env=env,
    )


def test_flaky_command_end_to_end(plain_repo, tmp_path):
    # A marker-flipping script: fails when the marker is absent (and creates
    # it), passes when present (and removes it). Across 4 runs it alternates
    # fail/pass/fail/pass → mixed → flaky_suspect.
    marker = os.path.join(plain_repo, ".flake_marker")
    script = (
        f'if [ -f "{marker}" ]; then rm "{marker}"; exit 0; '
        f'else : > "{marker}"; exit 1; fi'
    )
    config_path = _write_config(tmp_path, script)

    result = _run_precheck(plain_repo, config_path, "--flake-retries", "3")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["flake_runs_total"] == 4
    assert payload["flaky_suspect"] is True
    assert 0 < payload["flake_failures"] < 4


def test_default_run_has_no_flake_keys_end_to_end(plain_repo, tmp_path):
    config_path = _write_config(tmp_path, "false")
    result = _run_precheck(plain_repo, config_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pre_existing"] is True
    assert "flaky_suspect" not in payload
