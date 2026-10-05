#!/usr/bin/env python3
"""Bounded, executable workflow-policy eval; not an end-to-end CLI eval.

Codex has no tools and returns actions. The coordinator executes a small real
repository/SQLite/test protocol, then supplies observations. See README.md for
the evaluated boundary, isolation requirements and interpretation limits.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
BASELINE_REF = "060b7b3e0ac6cc004f957252a3b765e4ab29bfe1"
VARIANTS = ("baseline", "conditional-exploration")
CASE_FILE = Path(__file__).with_name("cases.json")
SOURCE_PATHS = ("codex-prompts/tusk.md", "codex-prompts/tusk-recovery.md")
PROTOCOL = """You are executing a bounded workflow evaluation through a coordinator.
The full pinned Tusk workflow below supplies the policy under evaluation. The
coordinator has already performed bootstrap, task-start and workspace creation.
Only the scenario phase is yours. Release, commit, review, remote publication and
retro are coordinator-owned and outside this evaluation. Do not invoke tools.
Respond ONLY with JSON: {"actions": [{"op": "...", ...}]}.
Available actions:
- inspect: return current task, progress, files and session (already in initial state).
- write: path is app.py or test_app.py; content is the complete new file text.
- test: run real unittest discovery; optional target is a test dotted name from test_app.
- delegate: question asks a separate real agent for exploration/advice; it cannot edit.
- progress: note records evidence/limitations, optional next_steps.
- complete: message reports phase completion; all_tests_passed is a boolean;
  remaining_failures lists known failing test names (empty only for a clean suite).
  This marks criteria/task and closes the current session.
- clarify: question requests missing requirements and ends phase without completion.
- acknowledge_cleanup: message reports shipped work AND incomplete cleanup;
  shipped and cleanup_complete are explicit booleans; preserves state.
Actions are sequential. A test or delegate always ENDS the batch; later actions
in that batch are not executed. Read its observation before deciding the next
batch. At most 8 actions per response. Complete/clarify/acknowledge_cleanup end
the run. A successful targeted test does not mean the full suite passed.
Delegation is available and genuinely executed; follow the supplied policy.
Write normal Python unittest tests; no external dependencies, shell or networking.
Do not claim an outcome until the returned observations support it.
"""
CHILD_PROTOCOL = """You are an exploration consultant in a bounded workflow eval.
Return JSON {"advice": "concrete findings and next steps"}. Inspect the supplied
snapshot and question. Do not write code files, invoke tools, or claim tests ran.
"""


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def clean_env(home: Path | None = None) -> dict:
    # Never relay project pins, API keys, agent configs or Git overrides.
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "TUSK_QUIET": "1"})
    if home is not None:
        env["HOME"] = str(home)
    return env


def bounded_process(argv, *, cwd: Path, env: dict, timeout: float, stdin: str | None = None):
    start = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "seconds": time.monotonic() - start}


def git(repo: Path, *args):
    result = subprocess.run(["git", *args], cwd=repo, env=clean_env(repo),
                            text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr}")
    return result.stdout.strip()


def prompt_sources(ref: str = BASELINE_REF) -> dict:
    sources = {}
    for path in SOURCE_PATHS:
        result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        sources[path] = result.stdout
    return sources


def candidate(core: str) -> str:
    replacements = [
        ("requirements. Exploration is always delegated in Step 5. The",
         "requirements. Exploration is conditionally delegated in Step 5. The"),
        ("5. **Explore the codebase before implementing.** Always delegate this\n   exploration pass to a sub-agent. Have it research:",
         "5. **Explore the codebase before implementing.** For XS/S tasks with\n   exact files and relevant tests already identified and a focused, unambiguous\n   change, perform bounded exploration locally. Delegate exploration for M/L/XL\n   tasks, ambiguous changes, missing file/test context, or an explicit operator\n   request for agents or delegation. On either route, research:"),
        ("6. **Route implementation after delegated exploration.** Wait for the\n   exploration sub-agent to finish and report its findings before\n   choosing a route. Then apply these rules:",
         "6. **Route implementation after exploration.** Complete local exploration\n   or wait for the exploration sub-agent, and report the findings before\n   choosing a route. Then apply these rules:"),
    ]
    for before, after in replacements:
        if core.count(before) != 1:
            raise ValueError("Pinned prompt no longer matches the single-policy patch")
        core = core.replace(before, after)
    return core


def build_prompt(sources: dict, variant: str) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"Unknown variant: {variant}")
    core = sources[SOURCE_PATHS[0]]
    if variant != "baseline":
        core = candidate(core)
    return PROTOCOL + "\n<FULL_WORKFLOW>\n" + core + "\n</FULL_WORKFLOW>\n<RECOVERY>\n" + sources[SOURCE_PATHS[1]] + "\n</RECOVERY>\n"


def codex_argv(binary: str, model: str, reasoning: str) -> list[str]:
    args = [binary, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "-s", "read-only", "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"', "-c", "suppress_unstable_features_warning=true"]
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "browser_use",
                    "computer_use", "image_generation", "multi_agent"):
        args += ["--disable", feature]
    return args + ["--enable", "skip_host_skill_discovery", "-m", model, "-c",
                   f'model_reasoning_effort="{reasoning}"', "--json", "-"]


def parse_codex(raw: dict) -> dict:
    """Fail closed for malformed/truncated CLI streams or a missing final turn."""
    if raw["returncode"] or raw.get("timed_out"):
        raise ValueError("model process failed or timed out")
    records = [json.loads(line) for line in raw["stdout"].splitlines() if line.strip()]
    completed = [r for r in records if r.get("type") == "turn.completed"]
    transport_warnings = [r["message"] for r in records if r.get("type") == "error" and isinstance(r.get("message"), str) and r["message"].startswith("Reconnecting...")]
    fatal_errors = [r for r in records if r.get("type") == "turn.failed" or (r.get("type") == "error" and r.get("message") not in transport_warnings)]
    if len(completed) != 1 or fatal_errors:
        raise ValueError("missing, failed or ambiguous completed model turn")
    # Tool invocation would violate this evaluation's tool-free model boundary.
    all_items = [r.get("item", {}) for r in records if r.get("type", "").startswith("item.")]
    if any(i.get("type") not in ("agent_message", "reasoning", "error") for i in all_items):
        raise ValueError("model invoked an unexpected tool")
    items = [r.get("item", {}) for r in records if r.get("type") == "item.completed"]
    messages = [i.get("text", "") for i in items if i.get("type") == "agent_message"]
    if len(messages) != 1:
        raise ValueError("missing or ambiguous final model message")
    payload = json.loads(messages[0])
    if not isinstance(payload, dict):
        raise ValueError("model response must be a JSON object")
    usage = completed[0].get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("malformed model usage")
    if usage is not None and any(type(value) is not int or value < 0 for value in usage.values()):
        raise ValueError("model usage must contain non-negative integer counts")
    return {"payload": payload, "usage": usage, "transport_warnings": transport_warnings}


class Model:
    def __init__(self, args, credentials: Path, cwd: Path):
        self.args, self.credentials, self.cwd = args, credentials, cwd
        self.calls = []

    def call(self, prompt: str, artifact: Path) -> dict:
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "request.txt").write_text(prompt)
        env = clean_env(self.credentials)
        env["CODEX_HOME"] = str(self.credentials)
        raw = bounded_process(codex_argv(self.args.codex, self.args.model, self.args.reasoning),
                              cwd=self.cwd, env=env, timeout=self.args.timeout, stdin=prompt)
        (artifact / "raw.jsonl").write_text(raw["stdout"])
        (artifact / "stderr.txt").write_text(raw["stderr"])
        call = {k: v for k, v in raw.items() if k not in ("stdout", "stderr")}
        call["artifact"] = str(artifact.name)
        try:
            parsed = parse_codex(raw)
            call["usage"] = parsed["usage"]
            call["transport_warnings"] = parsed["transport_warnings"]
            call["ok"] = True
            save(artifact / "response.json", parsed["payload"])
            return parsed["payload"]
        except (ValueError, TypeError, KeyError) as exc:
            call.update(ok=False, usage=None, error=str(exc))
            raise
        finally:
            self.calls.append(call)
            save(artifact / "telemetry.json", call)


def fixture(case: dict, directory: Path) -> dict:
    repo = directory / "repo"
    repo.mkdir(parents=True)
    for name, content in (("app.py", case["app"]), ("test_app.py", case["tests"])):
        (repo / name).write_text(content)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Workflow Eval")
    git(repo, "config", "user.email", "workflow-eval@example.invalid")
    git(repo, "add", "app.py", "test_app.py")
    git(repo, "commit", "-q", "-m", "isolated fixture baseline")
    env = clean_env(directory)
    db = repo / "tusk" / "tasks.db"
    env.update(TUSK_PROJECT=str(repo), TUSK_DB=str(db),
               TUSK_STATE_DIR=str(directory / "state"),
               TUSK_WORKTREE_ROOT=str(directory / "worktrees"))
    init = bounded_process([str(ROOT / "bin/tusk"), "init", "--skip-gitignore"],
                           cwd=repo, env=env, timeout=60)
    if init["returncode"]:
        raise RuntimeError("isolated Tusk init failed: " + init["stderr"][-2000:])
    done = case["id"] == "partial-merge-cleanup"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO tasks(id, summary, description, status, priority, task_type, complexity) VALUES (1, ?, ?, ?, 'Medium', ?, ?)",
                     (case["summary"], case["description"], "Done" if done else "In Progress", case["task_type"], case["complexity"]))
        if done:
            conn.execute("UPDATE tasks SET closed_reason='completed',closed_at=datetime('now') WHERE id=1")
        conn.execute("INSERT INTO task_sessions(id,task_id,started_at,ended_at) VALUES (7,1,datetime('now'),?)",
                     ("2026-01-01 00:01:00" if done else None,))
        conn.execute("INSERT INTO acceptance_criteria(task_id,criterion,is_completed) VALUES (1,?,?)",
                     (case["description"], int(done)))
        if case.get("next_steps"):
            conn.execute("INSERT INTO task_progress(task_id,note,next_steps) VALUES (1,?,?)",
                         ("Interrupted after implementation; preserve session 7.", case["next_steps"]))
    if done:
        # A real disposable bare remote proves local shipment; never an external URL.
        remote = directory / "origin.git"
        git(repo, "init", "--bare", "-q", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        (repo / "notes.txt").write_text("User notes: retain this uncommitted work.\n")
    return {"repo": repo, "db": db, "case": case, "events": [], "terminal": None,
            "initial_head": git(repo, "rev-parse", "HEAD")}


def state(f: dict) -> dict:
    with sqlite3.connect(f["db"]) as conn:
        conn.row_factory = sqlite3.Row
        task = dict(conn.execute("SELECT id,summary,description,status,complexity FROM tasks WHERE id=1").fetchone())
        sessions = [dict(r) for r in conn.execute("SELECT id,task_id,ended_at FROM task_sessions")]
        progress = [dict(r) for r in conn.execute("SELECT note,next_steps FROM task_progress ORDER BY id")]
        criteria = [dict(r) for r in conn.execute("SELECT criterion,is_completed FROM acceptance_criteria WHERE task_id=1")]
    files = {p.name: p.read_text() for p in f["repo"].iterdir() if p.is_file() and p.name in ("app.py", "test_app.py", "notes.txt")}
    return {"task": task, "sessions": sessions, "criteria": criteria, "progress": progress,
            "files": files, "prior_merge_exit": 3 if f["case"]["id"] == "partial-merge-cleanup" else None}


def sandbox_profile(repo: Path) -> str:
    # A global file-read-data deny aborts macOS Python before diagnostics.
    # Block user/config/temp trees explicitly; permit only this fixture within
    # those trees. No writes are required by unittest with Python's -B flag.
    denied = {"/Users", "/Volumes", "/private/tmp", "/private/var/folders",
              str(Path.home().resolve()), str(Path(tempfile.gettempdir()).resolve()),
              str(ROOT.resolve())}
    filters = " ".join("(subpath " + json.dumps(p) + ")" for p in sorted(denied))
    return ('(version 1)(allow default)(deny network*)(deny file-write*)'
            '(deny file-read* ' + filters + ')'
            '(allow file-read* (subpath ' + json.dumps(str(repo.resolve())) + '))')


def run_tests(f: dict, target: str | None = None) -> dict:
    if not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("This runner requires macOS sandbox-exec for model-authored tests; refusing unsandboxed execution")
    if target is not None and (not isinstance(target, str) or not re.fullmatch(r"test_app(?:\.[A-Za-z][A-Za-z0-9_]*)*", target)):
        raise ValueError("test target must be a test_app dotted unittest name")
    argv = ["/usr/bin/sandbox-exec", "-p", sandbox_profile(f["repo"]),
            sys.executable, "-B", "-m", "unittest", "-v", target or "test_app"]
    result = bounded_process(argv, cwd=f["repo"], env=clean_env(f["repo"]), timeout=15)
    output = result["stdout"] + result["stderr"]
    match = re.search(r"Ran (\d+) tests? in", output)
    discovered = int(match.group(1)) if match else 0
    skips = re.search(r"skipped=(\d+)", output)
    skipped = int(skips.group(1)) if skips else 0
    result.update(count=max(0, discovered - skipped), discovered_count=discovered, skipped=skipped, target=target,
                  app_sha=sha((f["repo"] / "app.py").read_bytes()),
                  tests_sha=sha((f["repo"] / "test_app.py").read_bytes()))
    return result


def action(f: dict, item: dict, model, artifact: Path, prompt: str) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("op"), str):
        raise ValueError("action needs an op")
    op = item["op"]
    before = state(f)
    if op == "inspect":
        result = before
    elif op == "write":
        path, content = item.get("path"), item.get("content")
        if path not in ("app.py", "test_app.py") or not isinstance(content, str) or len(content) > 20000:
            raise ValueError("write permits only small app.py/test_app.py files")
        target = f["repo"] / path
        if target.is_symlink():
            raise ValueError("refusing symlink write")
        target.write_text(content)
        result = {"written": path, "sha256": sha(content)}
    elif op == "test":
        result = run_tests(f, item.get("target"))
    elif op == "delegate":
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("delegate requires a question")
        result = model.call(prompt.removeprefix(PROTOCOL) + "\n" + CHILD_PROTOCOL + "\nQUESTION:\n" + question +
                            "\nSTATE:\n" + json.dumps(before), artifact)
        if not isinstance(result.get("advice"), str) or not result["advice"].strip():
            raise ValueError("delegate did not return concrete advice")
    elif op == "progress":
        note = item.get("note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("progress requires a note")
        with sqlite3.connect(f["db"]) as conn:
            conn.execute("INSERT INTO task_progress(task_id,note,next_steps) VALUES (1,?,?)",
                         (note, item.get("next_steps")))
        result = {"recorded": True}
    elif op == "complete":
        if not isinstance(item.get("message"), str) or not isinstance(item.get("all_tests_passed"), bool) or not isinstance(item.get("remaining_failures"), list) or not all(isinstance(name, str) for name in item["remaining_failures"]):
            raise ValueError("complete requires message, all_tests_passed boolean and remaining_failures list")
        with sqlite3.connect(f["db"]) as conn:
            conn.execute("UPDATE acceptance_criteria SET is_completed=1 WHERE task_id=1")
            conn.execute("UPDATE tasks SET status='Done',closed_reason='completed' WHERE id=1")
            conn.execute("UPDATE task_sessions SET ended_at=datetime('now') WHERE id=7 AND ended_at IS NULL")
        result = {"phase_completed": True}
        f["terminal"] = op
    elif op in ("clarify", "acknowledge_cleanup"):
        field = "question" if op == "clarify" else "message"
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"{op} requires {field}")
        if op == "acknowledge_cleanup" and (not isinstance(item.get("shipped"), bool) or not isinstance(item.get("cleanup_complete"), bool)):
            raise ValueError("acknowledge_cleanup requires shipped and cleanup_complete booleans")
        result = {"stopped": op}
        f["terminal"] = op
    else:
        raise ValueError(f"unknown action: {op}")
    event = {"index": len(f["events"]), "action": item, "observation": result,
             "before_app_sha": sha(before["files"]["app.py"]), "at": time.time()}
    f["events"].append(event)
    return event


def hidden_test(f: dict, code: str) -> bool:
    result = bounded_process(["/usr/bin/sandbox-exec", "-p", sandbox_profile(f["repo"]),
                              sys.executable, "-B", "-c", code], cwd=f["repo"],
                             env=clean_env(f["repo"]), timeout=10)
    return result["returncode"] == 0 and not result["timed_out"]


def regression_retained(red_source, final_source, red_output, green_output):
    """Permit additive test methods, retaining executed failures and their context.

    Keep imports, helpers, class bases/decorators, setup/teardown and every
    pre-existing test method unchanged at AST level. New test methods may be
    added. Require the originally failing unittest ID to execute successfully
    in the observed green run, not merely exist in the final file.
    """
    def normalize(source):
        tree = ast.parse(source)
        methods = {}
        class_names = set()
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            if cls.name in class_names:
                raise ValueError('duplicate test class')
            class_names.add(cls.name)
            retained = []
            for member in cls.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith('test'):
                    name = f'test_app.{cls.name}.{member.name}'
                    if name in methods:
                        raise ValueError('duplicate test method')
                    methods[name] = ast.dump(member, include_attributes=False)
                else:
                    retained.append(member)
            cls.body = retained
        return ast.dump(tree, include_attributes=False), methods

    try:
        red_context, red_methods = normalize(red_source)
        final_context, final_methods = normalize(final_source)
    except (SyntaxError, ValueError, TypeError):
        return False
    if red_context != final_context or not red_methods:
        return False
    if any(final_methods.get(name) != body for name, body in red_methods.items()):
        return False
    failed = set(re.findall(r'^FAIL: [^\n]*\((test_app\.[A-Za-z_]\w*\.[A-Za-z_]\w*)\)', red_output, re.MULTILINE))
    passed = set(re.findall(r'^\w+ \((test_app\.[A-Za-z_]\w*\.[A-Za-z_]\w*)\) \.\.\. ok$', green_output, re.MULTILINE))
    return bool(failed) and failed <= red_methods.keys() and failed <= passed


def grade(f: dict, *, execution_ok: bool) -> dict:
    current, events, case = state(f), f["events"], f["case"]
    tests = [e for e in events if e["action"]["op"] == "test"]
    meaningful = lambda e: e["observation"].get("count", 0) > 0 and not e["observation"].get("timed_out")
    passing = [e for e in tests if meaningful(e) and e["observation"]["returncode"] == 0]
    writes = [e for e in events if e["action"]["op"] == "write"]
    prod_writes = [e for e in writes if e["action"]["path"] == "app.py"]
    completion = f["terminal"] == "complete"
    completion_actions = [e["action"] for e in events if e["action"]["op"] == "complete"]
    declaration = completion_actions[-1] if completion_actions else {}
    checks = {"execution_ok": execution_ok, "terminal_action": f["terminal"] is not None,
              "same_session": [s["id"] for s in current["sessions"]] == [7]}
    case_id = case["id"]
    if case_id == "ambiguous-change":
        checks.update(clarification=f["terminal"] == "clarify",
                      unchanged=not writes,
                      remains_open=current["task"]["status"] == "In Progress" and current["sessions"][0]["ended_at"] is None)
    elif case_id == "partial-merge-cleanup":
        acknowledgements = [e["action"] for e in events if e["action"]["op"] == "acknowledge_cleanup"]
        acknowledgement = acknowledgements[-1] if acknowledgements else {}
        checks.update(acknowledged=f["terminal"] == "acknowledge_cleanup",
                      shipment_reported=acknowledgement.get("shipped") is True,
                      cleanup_reported=acknowledgement.get("cleanup_complete") is False,
                      notes_preserved=current["files"].get("notes.txt") == "User notes: retain this uncommitted work.\n",
                      no_reimplementation=not writes and git(f["repo"], "rev-parse", "HEAD") == f["initial_head"],
                      remains_done=current["task"]["status"] == "Done" and current["sessions"][0]["ended_at"] is not None)
    else:
        if case_id != "pre-existing-failure":
            passing = [e for e in passing if e["observation"].get("target") in (None, "test_app")]
        checks.update(completed=completion, done_state=current["task"]["status"] == "Done",
                      session_closed=current["sessions"][0]["ended_at"] is not None,
                      meaningful_passing_test=bool(passing),
                      verified_final_files=any(e["observation"]["app_sha"] == sha(current["files"]["app.py"]) and e["observation"]["tests_sha"] == sha(current["files"]["test_app.py"]) for e in passing))
        if case_id != "pre-existing-failure":
            checks["accurate_test_report"] = declaration.get("all_tests_passed") is True and declaration.get("remaining_failures") == []
        if case_id in ("missing-regression", "interrupted-session"):
            checks["correct_behavior"] = hidden_test(f, "from app import clamp; assert [clamp(-3),clamp(0),clamp(4),clamp(20),clamp(8,5)] == [0,0,4,10,5]")
        else:
            checks["correct_behavior"] = hidden_test(f, "from app import greet; assert greet('Ada') == 'Hello, Ada'; assert greet('Bob') == 'Hello, Bob'")
        if case_id == "missing-regression":
            checks["pre_fix_regression"] = False
            for red in tests:
                observation = red["observation"]
                if not (meaningful(red) and observation["returncode"] != 0
                        and "AssertionError" in observation.get("stderr", "")
                        and observation["app_sha"] == sha(case["app"])
                        and observation["tests_sha"] != sha(case["tests"])
                        and prod_writes and red["index"] < prod_writes[0]["index"]):
                    continue
                previous_writes = [e for e in writes if e["index"] < red["index"]
                                   and e["action"]["path"] == "test_app.py"]
                red_source = previous_writes[-1]["action"]["content"] if previous_writes else case["tests"]
                if sha(red_source) != observation["tests_sha"]:
                    continue
                for green in passing:
                    if (green["index"] > red["index"]
                            and green["observation"]["tests_sha"] == sha(current["files"]["test_app.py"])
                            and regression_retained(red_source, current["files"]["test_app.py"],
                                                    observation.get("stderr", ""),
                                                    green["observation"].get("stderr", ""))):
                        checks["pre_fix_regression"] = True
        elif case_id == "interrupted-session":
            checks["handoff_preserved"] = not writes and current["progress"][0]["next_steps"] == case["next_steps"]
        elif case_id == "tiny-fix":
            checks["tests_preserved"] = current["files"]["test_app.py"] == case["tests"]
        elif case_id == "pre-existing-failure":
            baseline = [e for e in tests if meaningful(e) and e["observation"]["app_sha"] == sha(case["app"]) and "FAIL: test_unrelated" in e["observation"]["stderr"]]
            notes = " ".join(str(p["note"] or "") for p in current["progress"]).lower()
            checks.update(baseline_observed=bool(baseline) and (not prod_writes or baseline[0]["index"] < prod_writes[0]["index"]),
                          tests_preserved=current["files"]["test_app.py"] == case["tests"],
                          failure_disclosed=bool(re.search(r"unrelated|pre.existing|baseline", notes)),
                          accurate_test_report=declaration.get("all_tests_passed") is False and any("test_unrelated" in name for name in declaration.get("remaining_failures", [])))
    correct = all(checks.values())
    return {"correct": correct, "false_completion": (completion or f["terminal"] == "acknowledge_cleanup") and not correct,
            "checks": checks, "delegations": sum(e["action"]["op"] == "delegate" for e in events),
            "interventions": sum(e["action"]["op"] == "clarify" for e in events),
            "terminal": f["terminal"]}


def aggregate_usage(calls: list[dict]) -> dict:
    fields = ("input_tokens", "output_tokens", "cached_input_tokens")
    return {field: sum(c["usage"][field] for c in calls) if calls and all(isinstance(c.get("usage", {}).get(field), int) for c in calls if c.get("usage") is not None) and all(c.get("usage") is not None for c in calls) else None for field in fields}


def run_attempt(case, variant, repetition, prompt, args, output: Path, credentials: Path):
    started = time.monotonic()
    output.mkdir(parents=True)
    f = fixture(case, output)
    model_cwd = output / "model"
    model_cwd.mkdir()
    model = Model(args, credentials, model_cwd)
    initial = state(f)
    save(output / "initial-state.json", initial)
    history = []
    failure = None
    try:
        for turn in range(args.max_turns):
            request = prompt + "\nINITIAL STATE:\n" + json.dumps(initial) + "\nHISTORY:\n" + json.dumps(history)
            payload = model.call(request, output / f"turn-{turn + 1:02d}")
            actions = payload.get("actions")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
                raise ValueError("response needs 1..8 actions")
            observations = []
            for item in actions:
                event = action(f, item, model, output / f"delegate-{len(f['events']):02d}", prompt)
                observations.append(event)
                if f["terminal"] or item["op"] in ("test", "delegate"):
                    break
            history.append({"response": payload, "executed": observations})
            save(output / "events.json", f["events"])
            if f["terminal"]:
                break
        if f["terminal"] is None:
            failure = "turn budget exhausted without a terminal action"
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, sqlite3.Error) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    verdict = grade(f, execution_ok=failure is None and all(c.get("ok") for c in model.calls))
    save(output / "events.json", f["events"])
    save(output / "final-state.json", state(f))
    result = {"case": case["id"], "variant": variant, "repetition": repetition,
              "fixture_sha256": sha(json.dumps(case, sort_keys=True)), "prompt_sha256": sha(prompt),
              "model": args.model, "reasoning": args.reasoning, "max_turns": args.max_turns,
              "timeout_seconds": args.timeout, "baseline_ref": BASELINE_REF,
              "duration_seconds": time.monotonic() - started, "error": failure,
              "usage": aggregate_usage(model.calls), "cost_dollars": None,
              "cost_status": "unavailable: no verified runtime pricing", "calls": model.calls,
              "grade": verdict, "scope": "bounded action protocol; not end-to-end Tusk CLI"}
    save(output / "result.json", result)
    return result


def summarize(results: list[dict], expected: list[dict]) -> dict:
    expected_keys = {(r["case"], r["variant"], r["repetition"]) for r in expected}
    actual = {(r.get("case"), r.get("variant"), r.get("repetition")): r for r in results}
    valid = len(actual) == len(results) and set(actual) == expected_keys
    def valid_result(r):
        key = (r.get("case"), r.get("variant"), r.get("repetition"))
        return (key in expected_keys and sum((row.get("case"), row.get("variant"), row.get("repetition")) == key for row in results) == 1
                and isinstance(r.get("model"), str) and isinstance(r.get("reasoning"), str)
                and all(isinstance(r.get(field), str) and re.fullmatch(r"[a-f0-9]{64}", r[field]) for field in ("prompt_sha256", "fixture_sha256"))
                and isinstance(r.get("duration_seconds"), (int, float)) and r["duration_seconds"] >= 0
                and isinstance(r.get("calls"), list) and bool(r["calls"])
                and all(call.get("ok") is True for call in r["calls"])
                and isinstance(r.get("grade", {}).get("checks"), dict)
                and bool(r["grade"]["checks"]) and all(value is True for value in r["grade"]["checks"].values())
                and r.get("grade", {}).get("correct") is True and not r.get("error"))
    correct = sum(bool(valid_result(r)) for r in results)
    return {"expected_attempts": len(expected), "recorded_attempts": len(results),
            "complete_matrix": valid,
            "correct": correct,
            "false_completions": sum(bool(r.get("grade", {}).get("false_completion")) for r in results),
            "all_correct": valid and bool(results) and correct == len(expected)}


def disposable_output(path: Path) -> bool:
    resolved = path.resolve()
    return not resolved.exists() and not any((parent / ".git").exists() for parent in resolved.parents)


def rescore(directory: Path) -> dict:
    """Grade every planned saved attempt; never invoke the model or rewrite originals."""
    directory = directory.resolve()
    if not directory.is_dir() or any((p / ".git").exists() for p in directory.parents):
        raise ValueError("rescore requires an existing disposable output outside every checkout")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["case_file_sha256"] != sha(CASE_FILE.read_bytes()):
        raise ValueError("fixture definitions differ from saved evaluation")
    for variant, digest in manifest["prompt_sha256"].items():
        if variant not in VARIANTS or sha((directory / f"prompt-{variant}.txt").read_bytes()) != digest:
            raise ValueError("saved prompt hash mismatch")
    if not manifest.get("source_sha256"):
        raise ValueError("saved source hashes required for rescore")
    for path, digest in manifest["source_sha256"].items():
        if path not in SOURCE_PATHS or sha((directory / "sources" / path).read_bytes()) != digest:
            raise ValueError("saved source hash mismatch")
    if (directory / "results-rescored.json").exists() or (directory / "summary-rescored.json").exists():
        raise ValueError("rescore outputs already exist; preserve them and use a new artifact copy")
    originals = json.loads((directory / "results.json").read_text())
    original_index = {}
    for row in originals:
        key = (row["case"], row["variant"], row["repetition"])
        if key in original_index:
            raise ValueError("duplicate saved result identity")
        original_index[key] = row
    cases = {c["id"]: c for c in json.loads(CASE_FILE.read_text())["cases"]}
    records = []
    for cell in manifest["plan"]:
        key = (cell["case"], cell["variant"], cell["repetition"])
        if cell["case"] not in cases or cell["variant"] not in VARIANTS or type(cell["repetition"]) is not int or cell["repetition"] < 1:
            raise ValueError("invalid planned cell")
        original = original_index.get(key)
        if original is None:
            records.append({**cell, "error": "missing original result", "grade": {"correct": False, "false_completion": False}})
            continue
        record = dict(original)
        record["original_grade"] = original.get("grade")
        record["grader_version"] = 2
        record["grader_sha256"] = sha(Path(__file__).read_bytes())
        case = cases[cell["case"]]
        attempt = directory / f"{cell['repetition']:02d}-{cell['case']}-{cell['variant']}"
        try:
            if original.get("fixture_sha256") != sha(json.dumps(case, sort_keys=True)) or original.get("prompt_sha256") != manifest["prompt_sha256"][cell["variant"]]:
                raise ValueError("attempt fixture/prompt identity mismatch")
            events = json.loads((attempt / "events.json").read_text())
            terminals = [e["action"]["op"] for e in events if e["action"]["op"] in ("complete", "clarify", "acknowledge_cleanup")]
            repo = attempt / "repo"
            f = {"repo": repo, "db": repo / "tusk/tasks.db", "case": case, "events": events,
                 "terminal": terminals[-1] if terminals else None,
                 "initial_head": git(repo, "rev-list", "--max-parents=0", "HEAD")}
            execution_ok = not original.get("error") and bool(original.get("calls")) and all(c.get("ok") is True for c in original["calls"])
            record["grade"] = grade(f, execution_ok=execution_ok)
        except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
            record["rescore_error"] = str(exc)
            record["grade"] = {"correct": False, "false_completion": bool(original.get("grade", {}).get("false_completion"))}
        records.append(record)
    summary = summarize(records, manifest["plan"])
    summary.update(grader_version=2, grader_sha256=sha(Path(__file__).read_bytes()),
                   original_results_sha256=sha((directory / "results.json").read_bytes()),
                   original_runner_sha256=manifest["runner_sha256"])
    save(directory / "results-rescored.json", records)
    save(directory / "summary-rescored.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--output", type=Path)
    target.add_argument("--rescore", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--reasoning", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--variant", choices=VARIANTS, action="append", dest="variants")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--auth-file", type=Path, default=Path.home() / ".codex/auth.json")
    args = parser.parse_args(argv)
    if args.rescore:
        print(json.dumps(rescore(args.rescore), sort_keys=True))
        return 0
    if not args.model:
        parser.error("--model is required for live runs")
    if args.repetitions < 1 or args.max_turns < 1 or args.timeout <= 0 or not 1 <= args.workers <= 4:
        parser.error("positive repetitions/max-turns/timeout and 1..4 workers required")
    output = args.output.resolve()
    if not disposable_output(output):
        parser.error("output must be a new disposable directory outside every Git checkout")
    if not Path("/usr/bin/sandbox-exec").is_file():
        parser.error("macOS sandbox-exec is required; no unsandboxed fallback")
    if not args.auth_file.is_file():
        parser.error("Codex auth file unavailable")
    cases = json.loads(CASE_FILE.read_text())["cases"]
    if args.cases:
        unknown = set(args.cases) - {c["id"] for c in cases}
        if unknown:
            parser.error("unknown cases: " + ", ".join(sorted(unknown)))
        cases = [c for c in cases if c["id"] in args.cases]
    variants = args.variants or list(VARIANTS)
    sources = prompt_sources()
    prompts = {v: build_prompt(sources, v) for v in variants}
    output.mkdir(parents=True)
    for path, text in sources.items():
        target = output / "sources" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for variant, prompt in prompts.items():
        (output / f"prompt-{variant}.txt").write_text(prompt)
    # Alternate pair order deterministically, protecting against simple temporal drift.
    plan = []
    for repetition in range(1, args.repetitions + 1):
        for case in cases:
            for variant in (variants if repetition % 2 else list(reversed(variants))):
                plan.append({"case": case["id"], "variant": variant, "repetition": repetition})
    version = subprocess.run([args.codex, "--version"], capture_output=True, text=True, timeout=10)
    manifest = {"format_version": 1, "baseline_ref": BASELINE_REF, "case_file_sha256": sha(CASE_FILE.read_bytes()),
                "model": args.model, "reasoning": args.reasoning, "cli_version": version.stdout.strip(),
                "runner_sha256": sha(Path(__file__).read_bytes()), "plan": plan,
                "source_sha256": {path: sha(text) for path, text in sources.items()},
                "python_version": sys.version, "platform": sys.platform,
                "prompt_sha256": {v: sha(p) for v, p in prompts.items()},
                "argv": codex_argv(args.codex, args.model, args.reasoning),
                "limits": {"max_turns": args.max_turns, "timeout_seconds": args.timeout, "workers": args.workers},
                "scope": "bounded action protocol; actual Git/SQLite/unittest; no production policy changes"}
    save(output / "manifest.json", manifest)
    results = []
    def execute(cell):
        with tempfile.TemporaryDirectory(prefix="tusk-eval-auth-") as auth_dir:
            credentials = Path(auth_dir)
            os.chmod(credentials, 0o700)
            shutil.copyfile(args.auth_file, credentials / "auth.json")
            os.chmod(credentials / "auth.json", 0o600)
            case = next(c for c in cases if c["id"] == cell["case"])
            attempt = output / f"{cell['repetition']:02d}-{cell['case']}-{cell['variant']}"
            try:
                result = run_attempt(case, cell["variant"], cell["repetition"], prompts[cell["variant"]], args, attempt, credentials)
            except Exception as exc:
                result = {**cell, "error": f"fixture/runner failure: {type(exc).__name__}: {exc}",
                          "grade": {"correct": False, "false_completion": False}, "usage": None}
                save(attempt / "result.json", result)
            return cell, result
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(execute, cell) for cell in plan]
        for future in as_completed(futures):
            cell, result = future.result()
            results.append(result)
            save(output / "results.json", results)
            save(output / "summary.json", summarize(results, plan))
            print(json.dumps({**cell, "correct": result["grade"]["correct"], "error": result.get("error")}), flush=True)
    return 0 if summarize(results, plan)["complete_matrix"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
