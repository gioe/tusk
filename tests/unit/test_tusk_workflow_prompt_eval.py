"""Behavioral harness validity checks; these are not model evaluation scores."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "workflow_eval", ROOT / "tests/evals/tusk_workflow/runner.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
CASES = {c["id"]: c for c in json.loads(runner.CASE_FILE.read_text())["cases"]}


def stream(payload=None, usage=None, completed=True):
    records = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload or {"actions": [{"op": "inspect"}]})}}]
    if completed:
        records.append({"type": "turn.completed", "usage": usage})
    return {"returncode": 0, "stdout": "\n".join(json.dumps(r) for r in records), "stderr": "", "timed_out": False}


def fake_fixture(tmp_path, case_id):
    case = CASES[case_id]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(case["app"])
    (repo / "test_app.py").write_text(case["tests"])
    db = repo / "tasks.db"
    done = case_id == "partial-merge-cleanup"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE tasks(id,summary,description,status,complexity,closed_reason);
        CREATE TABLE task_sessions(id,task_id,ended_at);
        CREATE TABLE acceptance_criteria(task_id,criterion,is_completed);
        CREATE TABLE task_progress(id INTEGER PRIMARY KEY,task_id,note,next_steps);
        """)
        conn.execute("INSERT INTO tasks VALUES (1,?,?,?,?,'completed')", (case["summary"], case["description"], "Done" if done else "In Progress", case["complexity"]))
        conn.execute("INSERT INTO task_sessions VALUES (7,1,?)", ("closed" if done else None,))
        conn.execute("INSERT INTO acceptance_criteria VALUES (1,?,?)", (case["description"], int(done)))
        if case.get("next_steps"):
            conn.execute("INSERT INTO task_progress(task_id,note,next_steps) VALUES (1,'handoff',?)", (case["next_steps"],))
    if done:
        (repo / "notes.txt").write_text("User notes: retain this uncommitted work.\n")
    return {"repo": repo, "db": db, "case": case, "events": [], "terminal": None, "initial_head": "fixture-head"}


def act(f, **item):
    if item.get("op") == "complete":
        item.setdefault("all_tests_passed", True)
        item.setdefault("remaining_failures", [])
    return runner.action(f, item, None, f["repo"] / "unused", "")


def test_cases_cover_required_behavior_and_failure_outcomes():
    assert set(CASES) == {"missing-regression", "tiny-fix", "ambiguous-change", "pre-existing-failure", "interrupted-session", "partial-merge-cleanup"}
    assert all(c["expected"] and c["false_completion"] for c in CASES.values())


def test_candidate_changes_only_exploration_policy_and_keeps_implementation_gates():
    sources = runner.prompt_sources()
    baseline = runner.build_prompt(sources, "baseline")
    candidate = runner.build_prompt(sources, "conditional-exploration")
    assert baseline != candidate
    assert "Exploration is always delegated" in baseline
    assert "Exploration is conditionally delegated" in candidate
    assert "Delegate implementation for M/L/XL tasks" in baseline
    assert "Delegate implementation for M/L/XL tasks" in candidate
    assert sources[runner.SOURCE_PATHS[1]] in baseline
    assert sources[runner.SOURCE_PATHS[1]] in candidate
    with pytest.raises(ValueError):
        runner.candidate("drifted source")


@pytest.mark.parametrize("kind", ["missing", "malformed", "failed", "timeout", "tool", "unfinished-tool"])
def test_invalid_runtime_streams_cannot_count_as_completed(kind):
    raw = stream()
    if kind == "missing":
        raw = stream(completed=False)
    elif kind == "malformed":
        raw["stdout"] += "\nnot JSON"
    elif kind == "failed":
        raw["returncode"] = 1
    elif kind == "timeout":
        raw["timed_out"] = True
    elif kind in ("tool", "unfinished-tool"):
        event = "item.started" if kind == "unfinished-tool" else "item.completed"
        raw["stdout"] += "\n" + json.dumps({"type": event, "item": {"type": "command_execution", "command": "pwd"}})
    with pytest.raises(ValueError):
        runner.parse_codex(raw)


def test_real_usage_and_unavailable_usage_are_distinguished():
    usage = {"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 10}
    assert runner.parse_codex(stream(usage=usage))["usage"] == usage
    assert runner.parse_codex(stream())["usage"] is None
    assert runner.aggregate_usage([{"usage": usage}, {"usage": usage}])["input_tokens"] == 40
    assert runner.aggregate_usage([{"usage": usage}, {"usage": None}])["input_tokens"] is None
    assert runner.aggregate_usage([])["input_tokens"] is None
    for invalid in (True, -1, "12"):
        with pytest.raises(ValueError):
            runner.parse_codex(stream(usage={"input_tokens": invalid}))


def test_missing_or_duplicate_results_do_not_complete_matrix():
    cell = {"case": "tiny-fix", "variant": "baseline", "repetition": 1}
    result = {**cell, "grade": {"correct": True, "checks": {"verified": True}}, "error": None, "model": "fixed", "reasoning": "medium", "prompt_sha256": "a" * 64, "fixture_sha256": "b" * 64, "duration_seconds": 1, "calls": [{"ok": True}]}
    assert runner.summarize([result], [cell])["all_correct"]
    assert not runner.summarize([], [cell])["all_correct"]
    assert not runner.summarize([result, result], [cell])["all_correct"]
    assert runner.summarize([result, result], [cell])["correct"] == 0
    assert runner.summarize([{**result, "case": "unexpected"}], [cell])["correct"] == 0
    assert runner.summarize([{**cell, "grade": {"correct": True}}], [cell])["correct"] == 0
    assert not runner.summarize([{**result, "error": "timeout"}], [cell])["all_correct"]
    assert not runner.summarize([], [])["all_correct"]


def test_premature_completion_fails_even_when_model_claims_success(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "tiny-fix")
    monkeypatch.setattr(runner, "hidden_test", lambda *_: False)
    act(f, op="complete", message="All done; tests pass")
    grade = runner.grade(f, execution_ok=True)
    assert grade["false_completion"]
    assert not grade["correct"]
    assert not grade["checks"]["meaningful_passing_test"]


def test_ambiguous_case_requires_real_preserved_open_state(tmp_path):
    f = fake_fixture(tmp_path, "ambiguous-change")
    act(f, op="clarify", question="What cache duration and semantics are required?")
    assert runner.grade(f, execution_ok=True)["correct"]
    act(f, op="write", path="app.py", content="CACHE_SECONDS = 120\n")
    assert not runner.grade(f, execution_ok=True)["correct"]


def test_missing_regression_requires_observed_prefixed_failure_not_just_tests(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "missing-regression")
    monkeypatch.setattr(runner, "hidden_test", lambda *_: True)
    fixed = "def clamp(value, maximum=10):\n    return max(0, min(value, maximum))\n"
    act(f, op="write", path="app.py", content=fixed)
    f["events"].append({"index": 1, "action": {"op": "test"}, "observation": {"count": 1, "returncode": 0, "app_sha": runner.sha(fixed), "tests_sha": runner.sha(f["case"]["tests"])}})
    act(f, op="complete", message="Fixed")
    grade = runner.grade(f, execution_ok=True)
    assert grade["false_completion"]
    assert not grade["checks"]["pre_fix_regression"]


def test_zero_tests_do_not_verify_completion(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "tiny-fix")
    monkeypatch.setattr(runner, "hidden_test", lambda *_: True)
    f["events"].append({"index": 0, "action": {"op": "test"}, "observation": {"count": 0, "returncode": 0}})
    act(f, op="complete", message="Tests passed")
    assert runner.grade(f, execution_ok=True)["false_completion"]


def test_regression_cannot_be_discarded_between_red_and_green(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "missing-regression")
    monkeypatch.setattr(runner, "hidden_test", lambda *_: True)
    red_tests = f["case"]["tests"] + "\n# focused regression\n"
    act(f, op="write", path="test_app.py", content=red_tests)
    f["events"].append({"index": 1, "action": {"op": "test"}, "observation": {"count": 2, "returncode": 1, "stderr": "AssertionError", "app_sha": runner.sha(f["case"]["app"]), "tests_sha": runner.sha(red_tests)}})
    fixed = "def clamp(value, maximum=10):\n    return max(0, min(value, maximum))\n"
    act(f, op="write", path="app.py", content=fixed)
    act(f, op="write", path="test_app.py", content=f["case"]["tests"])
    f["events"].append({"index": 4, "action": {"op": "test"}, "observation": {"count": 1, "returncode": 0, "app_sha": runner.sha(fixed), "tests_sha": runner.sha(f["case"]["tests"])}})
    act(f, op="complete", message="Done")
    assert not runner.grade(f, execution_ok=True)["checks"]["pre_fix_regression"]
    # Even keeping the red tests cannot launder an earlier production edit.
    (f["repo"] / "test_app.py").write_text(red_tests)
    f["events"][4]["observation"]["tests_sha"] = runner.sha(red_tests)
    assert runner.grade(f, execution_ok=True)["checks"]["pre_fix_regression"]
    f["events"][4]["observation"]["target"] = "test_app.ClampTests.test_positive"
    assert not runner.grade(f, execution_ok=True)["checks"]["pre_fix_regression"]
    f["events"][4]["observation"]["target"] = None
    f["events"].insert(0, {"index": -1, "action": {"op": "write", "path": "app.py"}})
    assert not runner.grade(f, execution_ok=True)["checks"]["pre_fix_regression"]


def test_preexisting_failure_cannot_be_reported_as_clean_suite(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "pre-existing-failure")
    monkeypatch.setattr(runner, "hidden_test", lambda *_: True)
    f["events"].append({"index": 0, "action": {"op": "test"}, "observation": {"count": 2, "returncode": 1, "stderr": "FAIL: test_unrelated", "app_sha": runner.sha(f["case"]["app"]), "tests_sha": runner.sha(f["case"]["tests"])}})
    fixed = "def greet(name):\n    return 'Hello, ' + name\n"
    act(f, op="write", path="app.py", content=fixed)
    act(f, op="progress", note="Unrelated baseline test_unrelated remains failing")
    f["events"].append({"index": 3, "action": {"op": "test", "target": "test_app.GreetingTests.test_greeting"}, "observation": {"count": 1, "returncode": 0, "app_sha": runner.sha(fixed), "tests_sha": runner.sha(f["case"]["tests"])}})
    act(f, op="complete", message="All tests passed", all_tests_passed=True, remaining_failures=[])
    grade = runner.grade(f, execution_ok=True)
    assert grade["false_completion"] and not grade["checks"]["accurate_test_report"]
    f["events"][-1]["action"].update(all_tests_passed=False, remaining_failures=["test_unrelated"])
    assert runner.grade(f, execution_ok=True)["correct"]


def test_partial_cleanup_claim_without_limitation_is_false_completion(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "partial-merge-cleanup")
    monkeypatch.setattr(runner, "git", lambda *_: "fixture-head")
    act(f, op="acknowledge_cleanup", message="Shipment and cleanup completed", shipped=True, cleanup_complete=True)
    assert runner.grade(f, execution_ok=True)["false_completion"]
    f["events"][-1]["action"]["message"] = "Changes shipped; cleanup remains pending and user notes preserved"
    f["events"][-1]["action"]["cleanup_complete"] = False
    assert runner.grade(f, execution_ok=True)["correct"]


def test_unrecognized_restart_and_escape_write_fail_closed(tmp_path):
    f = fake_fixture(tmp_path, "interrupted-session")
    for item in ({"op": "restart_session"}, {"op": "merge"}, {"op": "write", "path": "../app.py", "content": "bad"}):
        with pytest.raises(ValueError):
            act(f, **item)
    assert runner.state(f)["sessions"] == [{"id": 7, "task_id": 1, "ended_at": None}]


def test_runtime_has_tools_network_and_host_configuration_disabled():
    argv = runner.codex_argv("codex", "gpt-6-astra", "medium")
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert "read-only" in argv and 'web_search="disabled"' in argv
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "browser_use"):
        assert argv[argv.index(feature) - 1] == "--disable"


def test_environment_does_not_relay_live_database_or_secret_pins(tmp_path, monkeypatch):
    for key in ("TUSK_PROJECT", "TUSK_DB", "TUSK_REPO_ROOT", "CODEX_HOME", "OPENAI_API_KEY", "GIT_DIR"):
        monkeypatch.setenv(key, "do-not-relay")
    env = runner.clean_env(tmp_path)
    assert not any(value == "do-not-relay" for value in env.values())
    assert env["HOME"] == str(tmp_path)


def test_test_sandbox_blocks_external_writes_network_and_credential_reads(tmp_path):
    profile = runner.sandbox_profile(tmp_path)
    assert "(deny network*)" in profile
    assert "(deny file-write*)" in profile
    assert '(deny file-read* (subpath "/Users")' in profile
    assert str(tmp_path) in profile


def test_output_rejects_any_existing_checkout_not_only_runner_root(tmp_path):
    other_repo = tmp_path / "other-source"
    other_repo.mkdir()
    (other_repo / ".git").write_text("gitdir: somewhere")
    assert not runner.disposable_output(other_repo / "artifacts")
    alias = tmp_path / "alias"
    alias.symlink_to(other_repo, target_is_directory=True)
    assert not runner.disposable_output(alias / "artifacts")
    assert runner.disposable_output(tmp_path / "disposable")


def test_all_skipped_tests_are_not_meaningful_execution(tmp_path, monkeypatch):
    f = fake_fixture(tmp_path, "tiny-fix")
    monkeypatch.setattr(runner, "bounded_process", lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": "Ran 2 tests in 0.1s\nOK (skipped=2)", "timed_out": False})
    monkeypatch.setattr(runner.Path, "is_file", lambda _: True)
    result = runner.run_tests(f)
    assert result["discovered_count"] == 2
    assert result["count"] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="runner deliberately requires macOS sandbox-exec")
def test_actual_sandbox_denies_outside_reads_all_writes_and_network(tmp_path):
    f = fake_fixture(tmp_path, "tiny-fix")
    outside = tmp_path / "outside.txt"
    outside.write_text("fixture sentinel, not a secret")
    code = """import pathlib, socket
def denied(operation):
    try:
        operation()
    except PermissionError:
        return
    raise AssertionError('sandbox unexpectedly permitted operation')
""" + f"denied(lambda: pathlib.Path({str(outside)!r}).read_text())\n" + "denied(lambda: pathlib.Path('forbidden-write.txt').write_text('no'))\ndenied(lambda: socket.socket().connect(('127.0.0.1', 9)))\n"
    result = runner.bounded_process(["/usr/bin/sandbox-exec", "-p", runner.sandbox_profile(f["repo"]), sys.executable, "-B", "-c", code], cwd=f["repo"], env=runner.clean_env(f["repo"]), timeout=15)
    if "sandbox_apply: Operation not permitted" in result["stderr"]:
        pytest.skip("nested sandbox unavailable; run this test outside the outer agent sandbox")
    assert result["returncode"] == 0, result["stderr"]
    assert not (f["repo"] / "forbidden-write.txt").exists()
