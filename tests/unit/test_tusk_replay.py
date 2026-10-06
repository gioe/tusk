"""Regression checks for replay isolation, evidence, and failure accounting."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest


EVAL = Path(__file__).resolve().parents[1] / "evals" / "tusk_replay"


def load_runner():
    spec = importlib.util.spec_from_file_location("historical_replay", EVAL / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def grade_stub(tmp_path, case, filename, code):
    repo = tmp_path / "candidate"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / filename).write_text(code)
    result = subprocess.run(
        [sys.executable, "-B", str(EVAL / "grader.py"), case, str(repo)],
        capture_output=True, text=True, timeout=30,
    )
    return result, json.loads(result.stdout)


def test_grader_rejects_suppressing_all_path_warnings(tmp_path):
    result, report = grade_stub(
        tmp_path, "inline-python", "tusk-task-brief.py",
        "def _stale_spec_warnings(*args): return []\n"
        "def _spec_paths(*args): return []\n",
    )
    assert result.returncode == 1
    assert report["correct"] is False
    assert report["checks"]["source_no_false_warning_0_0"] is True
    assert report["checks"]["real_missing_operands_0_0"] is False


def test_grader_rejects_cache_cleanup_noop(tmp_path):
    result, report = grade_stub(
        tmp_path, "nested-cache", "tusk-merge.py",
        "def _clean_generated_test_caches(path): return 0\n",
    )
    assert result.returncode == 1
    assert report["correct"] is False
    assert report["checks"]["root_and_nested_caches_removed"] is False
    assert report["checks"]["cache_only_real_worktree_removable"] is False
    assert report["checks"]["unknown_leftovers_block_real_removal"] is True


def test_grader_rejects_destructive_cleanup(tmp_path):
    result, report = grade_stub(
        tmp_path, "nested-cache", "tusk-merge.py",
        "import shutil\ndef _clean_generated_test_caches(path): shutil.rmtree(path)\n",
    )
    assert result.returncode == 1
    assert report["correct"] is False
    assert not all(report["checks"].values())


def test_grader_import_failure_is_not_a_behavioral_pass(tmp_path):
    result, report = grade_stub(
        tmp_path, "inline-python", "tusk-task-brief.py",
        "raise RuntimeError('broken candidate')\n",
    )
    assert result.returncode == 1
    assert report["checks"] == {"grader_executed": False}
    assert "broken candidate" in report["diagnostic"]


@pytest.fixture
def runner():
    return load_runner()


def raw_events(*events, returncode=0, timed_out=False):
    return {"stdout": "\n".join(json.dumps(event) for event in events),
            "returncode": returncode, "timed_out": timed_out}


def test_events_accept_real_commands_and_preserve_unknown_total_usage(runner):
    raw = raw_events(
        {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Verified."}},
        {"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 20}},
    )
    result = runner.parse_events(raw)
    assert result["execution_ok"] is True
    assert result["command_count"] == 1
    assert result["parent_reported_usage"]["input_tokens"] == 123
    assert result["total_usage"] is None


@pytest.mark.parametrize("raw", [
    raw_events({"type": "turn.failed", "error": "timeout"}),
    raw_events({"type": "turn.completed"}, returncode=1),
    raw_events({"type": "turn.completed"}, timed_out=True),
    raw_events({"type": "turn.completed"}, {"type": "turn.completed"}),
    raw_events({"type": "error", "message": "authentication failed"}, {"type": "turn.completed"}),
    raw_events({"type": "error", "message": "Reconnecting... 1/5"}),
    {"stdout": "not JSON", "returncode": 0, "timed_out": False},
])
def test_failed_or_truncated_stream_cannot_pass(runner, raw):
    assert runner.parse_events(raw)["execution_ok"] is False


def test_recovered_transport_warning_is_accepted_only_with_completed_turn(runner):
    result = runner.parse_events(raw_events(
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "turn.completed"},
    ))
    assert result["execution_ok"] is True


def test_output_rejects_existing_and_git_nested_directories(runner, tmp_path):
    assert not runner.disposable_output(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: elsewhere")
    assert not runner.disposable_output(checkout / "new-run")
    assert runner.disposable_output(tmp_path / "fresh-run")


def test_output_rejects_symlink_alias_into_checkout(runner, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (tmp_path / "alias").symlink_to(checkout, target_is_directory=True)
    assert not runner.disposable_output(tmp_path / "alias" / "new-run")


def test_environment_does_not_inherit_project_pins_or_credentials(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("TUSK_DB", "/live/tasks.db")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-copy")
    monkeypatch.setenv("GIT_DIR", "/live/.git")
    monkeypatch.setenv("PYTHONPATH", "/live/python")
    env = runner.fixture_env(tmp_path, tmp_path / "repo", tmp_path / "workspace")
    assert env["TUSK_DB"] == str(tmp_path / "repo/tusk/tasks.db")
    assert env["HOME"] == str(tmp_path / "home")
    assert not {"OPENAI_API_KEY", "GIT_DIR", "PYTHONPATH"}.intersection(env)


def test_summary_rejects_duplicate_cells_and_keeps_missing_denominator(runner):
    a = {"case": "inline-python", "variant": "full", "repetition": 1}
    b = {"case": "inline-python", "variant": "compact", "repetition": 1}
    duplicate = {**a, "correct": True}
    result = runner.summarize([duplicate, duplicate], [a, b])
    assert result["complete_matrix"] is False
    assert result["correct"] == 0
    assert result["failed_or_incomplete"] == 2


def test_summary_does_not_trust_unsubstantiated_success(runner):
    cell = {"case": "inline-python", "variant": "full", "repetition": 1}
    result = runner.summarize([{**cell, "correct": True}], [cell])
    assert result["correct"] == 0
    assert result["failed_or_incomplete"] == 1


def test_snapshot_has_no_future_history_or_host_agent_configuration(runner, tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    env = runner.clean_env(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=origin, env=env, text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Replay test")
    git("config", "user.email", "replay@example.invalid")
    (origin / "bug.py").write_text("broken\n")
    (origin / "AGENTS.md").write_text("obsolete instructions")
    (origin / ".claude").mkdir()
    (origin / ".claude/settings.json").write_text('{"hooks":"outside"}')
    git("add", ".")
    git("commit", "-qm", "original")
    before = git("rev-parse", "HEAD")
    (origin / "bug.py").write_text("known solution\n")
    git("commit", "-qam", "future solution")
    future = git("rev-parse", "HEAD")
    monkeypatch.setattr(runner, "ROOT", origin)
    replay = tmp_path / "snapshot"
    runner.archive_snapshot(before, replay, env)
    assert (replay / "bug.py").read_text() == "broken\n"
    assert not (replay / "AGENTS.md").exists()
    assert not (replay / ".claude").exists()
    hidden = subprocess.run(["git", "cat-file", "-e", future], cwd=replay, env=env,
                            capture_output=True)
    assert hidden.returncode != 0
    assert subprocess.check_output(["git", "rev-list", "--count", "HEAD"],
                                   cwd=replay, env=env, text=True).strip() == "1"


def test_profile_denies_sibling_temp_access_and_network(runner, tmp_path):
    execution = tmp_path.resolve() / "execution"
    execution.mkdir()
    args = runner.permission_args(execution)
    config = tomllib.loads("\n".join(args[1::2]))
    profile = config["permissions"]["replay"]
    assert profile["filesystem"][":root"] == "deny"
    assert profile["filesystem"][":slash_tmp"] == "deny"
    assert profile["filesystem"][str(execution)] == "write"
    assert profile["network"]["enabled"] is False


def test_postrun_profile_cannot_grant_external_git_symlink(runner, tmp_path):
    execution = tmp_path.resolve() / "execution"
    execution.mkdir()
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (execution / ".git").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        runner.permission_args(execution)


def test_postrun_profile_cannot_follow_replaced_execution_root(runner, tmp_path):
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    execution = tmp_path.resolve() / "execution"
    execution.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        runner.permission_args(execution)


def test_temp_adaptation_is_narrow_and_fail_closed(runner, tmp_path):
    (tmp_path / "bin").mkdir()
    cli = tmp_path / "bin/tusk"
    original = 'mktemp -t tusk-stderr.XXXXXX\nmktemp -t tusk-uv.XXXXXX\nother code\n'
    cli.write_text(original)
    runner.adapt_temporary_storage(tmp_path)
    assert cli.read_text().count('mktemp -p "${TMPDIR:?}" -t') == 2
    assert cli.read_text().endswith("other code\n")
    with pytest.raises(ValueError, match="no longer matches"):
        runner.adapt_temporary_storage(tmp_path)


def test_calibration_failure_records_every_planned_cell_without_model_calls(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(runner, "checked", lambda *args, **kwargs: "test source")

    def fail_fixture(*args, **kwargs):
        raise RuntimeError("sandbox unavailable")

    monkeypatch.setattr(runner, "make_fixture", fail_fixture)
    monkeypatch.setattr(runner, "run_attempt", lambda *args: pytest.fail("model run before calibration"))
    output = tmp_path / "calibration"
    status = runner.main(["--calibrate-only", "--output", str(output),
                          "--codex", "not-called", "--auth-file", str(tmp_path / "missing-auth")])
    assert status == 1
    results = json.loads((output / "results.json").read_text())
    assert len(results) == 4
    assert all(row["not_started"] and not row["correct"] for row in results)
    assert json.loads((output / "summary.json").read_text())["failed_or_incomplete"] == 4


def test_summary_accepts_complete_evidence_and_counts_absent_cells(runner):
    a = {"case": "inline-python", "variant": "full", "repetition": 1}
    b = {"case": "inline-python", "variant": "compact", "repetition": 1}
    good = {**a, "execution_ok": True, "phase_boundary_respected": True,
            "grade": {"correct": True, "checks": {"behavior": True}},
            "workflow_checks": {"implementation_commit": True, "progress_recorded": True,
                                "phase_boundary": True}}
    report = runner.summarize([good], [a, b])
    assert report["correct"] == 1
    assert report["failed_or_incomplete"] == 1
    assert report["complete_matrix"] is False


def test_real_tool_runner_keeps_native_thread_context(runner, tmp_path):
    execution = tmp_path.resolve()
    args = SimpleNamespace(codex="codex", model="test-model", reasoning="medium")
    fixture = {"execution": execution, "cwd": execution,
               "env": runner.fixture_env(execution, execution, execution)}
    command = runner.codex_argv(args, fixture)
    assert "--ephemeral" not in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "default_permissions=\"replay\"" in command
    assert "multi_agent" in command
    assert "--sandbox" not in command  # Would override restricted-read profile.


def test_tool_ledger_excludes_credentials_instructions_reasoning_and_previous_runs(runner, tmp_path):
    credentials = tmp_path / "credentials"
    sessions = credentials / "sessions"
    sessions.mkdir(parents=True)
    (credentials / "auth.json").write_text('{"secret":"never export"}')
    previous = sessions / "previous.jsonl"
    previous.write_text(json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "name": "prior_run", "arguments": "{}"}}) + "\n")
    allowed = {"type": "function_call_output", "call_id": "one", "output": "child started"}
    rows = [
        {"type": "response_item", "payload": allowed},
        {"type": "response_item", "payload": {"type": "reasoning", "text": "private"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": "instructions"}},
        {"type": "session_meta", "payload": {"instructions": "private"}},
    ]
    (sessions / "current.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    output = tmp_path / "artifact"
    assert runner.capture_tool_ledger(credentials, {previous}, output) == 1
    result = json.loads((output / "tool-ledger.json").read_text())
    assert result[0]["payload"] == allowed
    assert "secret" not in (output / "tool-ledger.json").read_text()
