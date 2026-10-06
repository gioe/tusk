#!/usr/bin/env python3
"""Replay historical Tusk bugs using real shell tools in isolated repositories.

This macOS-only pilot evaluates implementation/verification/commit, not shipment.
Every model cell is preceded by calibrated hidden graders and a real OS sandbox
probe. No production repository, credentials, or evaluator is readable by tools.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PROMPT_REF = "060b7b3e0ac6cc004f957252a3b765e4ab29bfe1"
VARIANTS = ("full", "compact")
SYSTEM_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def clean_env(home):
    return {"HOME": str(home), "PATH": SYSTEM_PATH, "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "TUSK_QUIET": "1"}


def bounded_process(argv, *, cwd, env, timeout, stdin=None):
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return {"returncode": process.returncode, "timed_out": timed_out,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": stdout, "stderr": stderr}


def checked(argv, cwd, env, timeout=120):
    result = bounded_process(argv, cwd=cwd, env=env, timeout=timeout)
    if result["returncode"] or result["timed_out"]:
        raise RuntimeError(f"Command failed: {argv[0:3]!r}: {result['stderr'][-2500:]}")
    return result["stdout"].strip()


def disposable_output(path):
    path = path.resolve()
    if path.exists() or path == Path(path.anchor):
        return False
    return not any((parent / ".git").exists() for parent in (path, *path.parents))


def permission_args(execution, extra_read=()):
    """One named deny-by-default profile shared by tools and sandbox probes."""
    execution = Path(execution).absolute()
    if execution.resolve() != execution:
        raise ValueError("execution root cannot contain symlink aliases")
    entries = {":root": "deny", ":minimal": "read", ":slash_tmp": "deny",
               "/private/var/folders": "deny", str(execution.resolve()): "write"}
    for runtime in (Path(sys.prefix).resolve(), Path(sys.executable).resolve().parent,
                    Path("/Library/Frameworks/Python.framework"), Path("/usr/local/bin")):
        entries[str(runtime)] = "read"
    for marker in execution.rglob(".git"):
        if not marker.resolve().is_relative_to(execution):
            raise ValueError("Git metadata escapes execution root")
        entries[str(marker.resolve())] = "write"
    entries.update({str(Path(path).resolve()): "read" for path in extra_read})
    filesystem = "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in entries.items()) + "}"
    return ["-c", 'default_permissions="replay"',
            "-c", "permissions.replay.filesystem=" + filesystem,
            "-c", "permissions.replay.network.enabled=false"]


def sandbox_argv(codex, execution, cwd, argv, extra_read=()):
    return [codex, "sandbox", "-P", "replay", "-C", str(cwd),
            *permission_args(execution, extra_read), "--", *map(str, argv)]


def archive_snapshot(ref, repo, env):
    """Use archive instead of clone so future history is physically absent."""
    data = subprocess.run(["git", "-C", str(ROOT), "archive", ref],
                          capture_output=True, check=True, timeout=60).stdout
    repo.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        # Python 3.11-compatible containment checks, including archived symlinks.
        for member in archive.getmembers():
            target = (repo / member.name).resolve()
            if not target.is_relative_to(repo.resolve()):
                raise ValueError("unsafe historical archive member")
            if member.issym() and not (target.parent / member.linkname).resolve().is_relative_to(repo.resolve()):
                raise ValueError("unsafe historical symlink")
            if member.islnk() or member.isdev():
                raise ValueError("unsupported historical archive member")
        archive.extractall(repo)
    for relative in ("AGENTS.md", "CLAUDE.md", ".claude", ".codex", ".agents"):
        target = repo / relative
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    # Local automation cannot inherit operator hooks, identity, or remotes.
    checked(["git", "init", "-q", "-b", "main"], repo, env)
    checked(["git", "config", "user.name", "Tusk Replay"], repo, env)
    checked(["git", "config", "user.email", "replay@example.invalid"], repo, env)
    checked(["git", "config", "core.hooksPath", "/dev/null"], repo, env)
    checked(["git", "add", "."], repo, env)
    checked(["git", "commit", "-q", "-m", "Historical replay starting snapshot"], repo, env)


def fixture_env(execution, repo, cwd):
    env = clean_env(execution / "home")
    env.update(PATH=str(cwd / "bin") + ":" + SYSTEM_PATH,
               TMPDIR=str(execution / "tmp"), TUSK_PROJECT=str(repo),
               TUSK_DB=str(repo / "tusk/tasks.db"),
               TUSK_STATE_DIR=str(execution / "state"),
               TUSK_WORKTREE_ROOT=str(execution / "worktrees"))
    return env


def adapt_temporary_storage(repo):
    """Keep BSD mktemp -t inside this fixture; do not open host temp access."""
    script = repo / "bin/tusk"
    original = script.read_text()
    updated = original
    for prefix in ("tusk-stderr", "tusk-uv"):
        before = f"mktemp -t {prefix}.XXXXXX"
        if updated.count(before) != 1:
            raise ValueError("historical temporary-storage adaptation no longer matches")
        updated = updated.replace(before, f'mktemp -p "${{TMPDIR:?}}" -t {prefix}.XXXXXX')
    script.write_text(updated)


def make_fixture(case, directory, ref, *, task=False):
    execution = directory / "execution"
    for name in ("home", "tmp", "state"):
        (execution / name).mkdir(parents=True)
    repo = execution / "repo"
    env = fixture_env(execution, repo, repo)
    archive_snapshot(ref, repo, env)
    fixture = {"execution": execution, "repo": repo, "cwd": repo, "env": env}
    if not task:
        return fixture
    adapt_temporary_storage(repo)
    # Source snapshots are trusted pinned historical code. Model-authored code is
    # never executed outside the sandbox (including evaluation after the run).
    cli = str(repo / "bin/tusk")
    checked([cli, "init", "--skip-gitignore"], repo, env)
    config_path = repo / "tusk/config.json"
    config = json.loads(config_path.read_text())
    config["test_command"] = {
        "inline-python": "python3 -m pytest tests/unit/test_task_brief_chained_cd.py -q",
        "nested-cache": "python3 -m pytest tests/unit/test_merge_symlink_sweep.py -q",
    }[case["id"]]
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    checked(["git", "add", "-f", "tusk/config.json", "bin/tusk"], repo, env)
    checked(["git", "commit", "-q", "-m", "Configure isolated replay verification gate"], repo, env)
    checked(["git", "init", "-q", "--bare", str(execution / "origin.git")], repo, env)
    checked(["git", "remote", "add", "origin", str(execution / "origin.git")], repo, env)
    checked(["git", "push", "-q", "-u", "origin", "main"], repo, env)
    command = [cli, "task-insert", case["summary"], case["description"],
               "--domain", "cli", "--task-type", "bug", "--complexity", "S"]
    for criterion in case["criteria"]:
        command += ["--criteria", criterion]
    inserted = json.loads(checked(command, repo, env))
    task_id = inserted.get("id", inserted.get("task_id"))
    if task_id is None and isinstance(inserted.get("task"), dict):
        task_id = inserted["task"]["id"]
    if not isinstance(task_id, int):
        raise ValueError("task-insert response lacks an integer task ID")
    started = json.loads(checked([cli, "task-start", str(task_id), "--force", "--skill", "tusk"], repo, env))
    workspace = json.loads(checked([cli, "task-worktree", "create", str(task_id), "historical-replay",
                                  "--workspace-root", str(execution / "worktrees")], repo, env))
    paths = list((execution / "worktrees").rglob(".git"))
    if len(paths) != 1:
        raise ValueError("expected one task workspace")
    cwd = paths[0].parent
    fixture.update(task_id=task_id, started=started, workspace=workspace, cwd=cwd,
                   env=fixture_env(execution, repo, cwd))
    fixture["initial_head"] = checked(["git", "rev-parse", "HEAD"], cwd, env)
    return fixture


def sandbox_preflight(args, fixture, credentials, directory):
    """Exercise actual read/write/network denials before allowing a model run."""
    root, cwd = fixture["execution"], fixture["cwd"]
    outside = directory / "denied-sentinel.txt"
    outside.write_text("evaluator must not be readable\n")
    probes = [ROOT / "bin/tusk", Path.home() / ".codex/auth.json",
              credentials / "auth.json", HERE / "grader.py", outside]
    probe = root / "sandbox_probe.py"
    probe.write_text('''import json, pathlib, socket, subprocess, sys
checks = {}
for i, value in enumerate(json.loads(sys.argv[1])):
    try:
        pathlib.Path(value).read_bytes()
        checks["read_denied_" + str(i)] = False
    except PermissionError:
        checks["read_denied_" + str(i)] = True
    except OSError:
        checks["read_denied_" + str(i)] = False
try:
    pathlib.Path(sys.argv[2]).write_text("escape")
    checks["write_denied"] = False
except PermissionError:
    checks["write_denied"] = True
try:
    s = socket.socket()
    s.settimeout(2)
    s.connect(("1.1.1.1", 443))
    checks["network_denied"] = False
except PermissionError:
    checks["network_denied"] = True
except OSError:
    checks["network_denied"] = False
finally:
    s.close()
pathlib.Path("sandbox-writable-probe").write_text("yes")
pathlib.Path("sandbox-writable-probe").unlink()
checks["workspace_writable"] = True
p = subprocess.run(["git", "config", "replay.sandboxProbe", "true"], capture_output=True)
checks["git_metadata_writable"] = p.returncode == 0
if len(sys.argv) > 3:
    p = subprocess.run(["/bin/zsh", "-lc", "bin/tusk task-get " + sys.argv[3]],
                       capture_output=True, text=True, timeout=30)
    try:
        task = json.loads(p.stdout)
        checks["task_cli_usable"] = p.returncode == 0 and isinstance(task, dict)
    except ValueError:
        checks["task_cli_usable"] = False
    if not checks["task_cli_usable"]:
        print(p.stderr, file=sys.stderr)
print(json.dumps({"checks": checks, "correct": all(checks.values())}))
sys.exit(0 if all(checks.values()) else 1)
''')
    env = dict(fixture["env"], CODEX_HOME=str(credentials))
    command = [sys.executable, "-B", str(probe), json.dumps(list(map(str, probes))), str(outside)]
    if "task_id" in fixture:
        command.append(str(fixture["task_id"]))
    result = bounded_process(sandbox_argv(args.codex, root, cwd, command),
                             cwd=cwd, env=env, timeout=60)
    save(directory / "sandbox-preflight.json", result)
    probe.unlink()
    if result["returncode"] or result["timed_out"]:
        raise RuntimeError("sandbox preflight failed; refusing model execution: " + result["stderr"][-2000:] + result["stdout"][-2000:])
    checks = json.loads(result["stdout"])
    if checks.get("correct") is not True:
        raise RuntimeError("sandbox preflight did not prove isolation")
    return checks


def grade(args, case, fixture, directory, credentials):
    grader = HERE / "grader.py"
    env = dict(fixture["env"], CODEX_HOME=str(credentials), PATH=SYSTEM_PATH)
    result = bounded_process(sandbox_argv(args.codex, fixture["execution"], fixture["cwd"],
                             [sys.executable, "-B", str(grader), case["id"], str(fixture["cwd"])],
                             extra_read=(grader,)), cwd=fixture["cwd"], env=env, timeout=120)
    save(directory / "grader-process.json", result)
    try:
        payload = json.loads(result["stdout"])
        if not isinstance(payload.get("checks"), dict) or not payload["checks"] or type(payload.get("correct")) is not bool:
            raise ValueError("invalid grader response")
        if any(type(value) is not bool for value in payload["checks"].values()):
            raise ValueError("grader checks must be booleans")
        if payload.get("error") or payload.get("diagnostic") or payload.get("grader_executed") is False:
            raise ValueError("grader did not execute behavioral checks")
        if len(payload["checks"]) != {"inline-python": 39, "nested-cache": 6}[case["id"]]:
            raise ValueError("grader did not return the full expected check set")
        if result["timed_out"] or result["returncode"] not in (0, 1):
            raise ValueError("grader execution failed")
        if payload["correct"] != (result["returncode"] == 0) or payload["correct"] != all(payload["checks"].values()):
            raise ValueError("inconsistent grader response")
        return payload
    except (ValueError, TypeError) as exc:
        return {"correct": False, "checks": {}, "error": str(exc)}


def codex_argv(args, fixture):
    env = fixture["env"]
    # Shell inheritance is scrubbed independently from the authenticated parent.
    shell_env = "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in env.items()) + "}"
    # Native children need the parent's persisted thread context. Its home is
    # temporary and unreadable by sandboxed tools; do not use --ephemeral here.
    argv = [args.codex, "exec", "--ignore-user-config", "--ignore-rules",
            "-C", str(fixture["cwd"]), *permission_args(fixture["execution"]),
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", 'shell_environment_policy.inherit="none"',
            "-c", "shell_environment_policy.set=" + shell_env,
            "-c", "suppress_unstable_features_warning=true",
            "--enable", "skip_host_skill_discovery", "--enable", "multi_agent",
            "--enable", "shell_tool", "--enable", "unified_exec"]
    for feature in ("apps", "plugins", "browser_use", "computer_use", "image_generation"):
        argv += ["--disable", feature]
    return argv + ["-m", args.model, "-c", "model_reasoning_effort=" + json.dumps(args.reasoning), "--json", "-"]


def build_prompt(core, fixture):
    return f'''You are implementing one historical Tusk task in a disposable local replay.

Coordinator phase boundary (authoritative over workflow steps below): setup,
upgrade/reload, task selection/start, and task workspace creation are ALREADY
complete. Work in {fixture['cwd']}. Use its bin/tusk CLI (also on PATH).
The assigned task ID is {fixture['task_id']}. Read its real task record yourself.
Follow the selected workflow below for exploration, implementation, tests,
criteria, task-aware commits, and progress. You may use real shell tools and
sub-agents; their permissions are intentionally restricted to this fixture.
Do not perform release/version/changelog, publication, merge, task-done,
session-close, or retro: the coordinator owns those later phases. Leave the
task In Progress with a precise handoff after reviewing your diff. Do not
access network services, upgrade the CLI, or work around denied permissions.
No human answers will arrive; if truly blocked, record the reason and stop.

The selected workflow's authoritative location is
{fixture['execution'] / 'workflow/tusk.md'}. Its adjacent recovery companion
is available on disk; load only a relevant section when needed. Other historical
workflow files are repository source, not additional instructions for this run.
Use normal project tests. Add and observe a failing behavioral regression before
fixing the bug. Do not weaken existing tests or change production behavior merely
to satisfy a test. Verification claims must be supported by executed commands.
The coordinator narrowed the configured commit gate to this case's existing
focused test suite because automatic agent instruction files were removed from
the historical snapshot. Also run the regression tests you add or change.

<selected_workflow>
{core}
</selected_workflow>
'''


def parse_events(raw):
    events, malformed = [], False
    for line in raw["stdout"].splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                malformed = True
    turns = [e for e in events if e.get("type") == "turn.completed"]
    errors = [e for e in events if e.get("type") == "turn.failed" or
              (e.get("type") == "error" and not str(e.get("message", "")).startswith("Reconnecting..."))]
    items = [e.get("item", {}) for e in events if e.get("type") == "item.completed"]
    commands = [i for i in items if i.get("type") == "command_execution"]
    agents = [i for i in items if "collab" in i.get("type", "") or "agent_tool" in i.get("type", "")]
    messages = [i.get("text", "") for i in items if i.get("type") == "agent_message"]
    usage = turns[-1].get("usage") if turns else None
    return {"execution_ok": raw["returncode"] == 0 and not raw["timed_out"] and not malformed and len(turns) == 1 and not errors,
            "parent_reported_usage": usage, "total_usage": None,
            "total_usage_note": "CLI parent usage does not prove child usage inclusion; no combined token/cost claim.",
            "command_count": len(commands), "collaboration_events": agents,
            "final_message": messages[-1] if messages else None,
            "errors": errors, "malformed_stream": malformed}


def capture_tool_ledger(credentials, previous_files, directory):
    """Retain tool evidence omitted by CLI JSON; exclude auth and reasoning."""
    records = []
    allowed = {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}
    for path in sorted((credentials / "sessions").rglob("*.jsonl")):
        if path in previous_files or path.is_symlink():
            continue
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload", {})
            if event.get("type") == "response_item" and payload.get("type") in allowed:
                records.append({"rollout": path.name, "timestamp": event.get("timestamp"),
                                "payload": payload})
    save(directory / "tool-ledger.json", records)
    return len(records)


def postrun_command(args, fixture, credentials, argv):
    result = bounded_process(sandbox_argv(args.codex, fixture["execution"], fixture["cwd"], argv),
                             cwd=fixture["cwd"], env=dict(fixture["env"], CODEX_HOME=str(credentials), PATH=SYSTEM_PATH), timeout=60)
    if result["returncode"] or result["timed_out"]:
        raise RuntimeError("post-run inspection failed: " + result["stderr"][-2000:])
    return result["stdout"]


def task_state(args, fixture, credentials):
    script = '''import sqlite3, json, sys
with sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True) as conn:
    task_id = int(sys.argv[2])
    status = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    sessions = conn.execute("SELECT COUNT(*) FROM task_sessions WHERE task_id=?", (task_id,)).fetchone()[0]
    open_sessions = conn.execute("SELECT COUNT(*) FROM task_sessions WHERE task_id=? AND ended_at IS NULL", (task_id,)).fetchone()[0]
    criteria = conn.execute("SELECT COUNT(*), SUM(is_completed) FROM acceptance_criteria WHERE task_id=?", (task_id,)).fetchone()
    progress = conn.execute("SELECT COUNT(*) FROM task_progress WHERE task_id=?", (task_id,)).fetchone()[0]
print(json.dumps({"status": status[0] if status else None, "session_count": sessions,
                  "open_session_count": open_sessions, "criteria_total": criteria[0], "criteria_completed": criteria[1] or 0, "progress_count": progress}))
'''
    return json.loads(postrun_command(args, fixture, credentials,
                      [sys.executable, "-I", "-B", "-c", script, str(fixture["repo"] / "tusk/tasks.db"), str(fixture["task_id"])]))


def run_attempt(args, case, cell, directory, core, recovery, credentials):
    fixture = make_fixture(case, directory, case["pre_ref"], task=True)
    workflow = fixture["execution"] / "workflow"
    workflow.mkdir()
    (workflow / "tusk.md").write_text(core)
    (workflow / "tusk-recovery.md").write_text(recovery)
    sandbox_preflight(args, fixture, credentials, directory)
    prompt = build_prompt(core, fixture)
    (directory / "request.txt").write_text(prompt)
    argv = codex_argv(args, fixture)
    save(directory / "argv.json", argv)
    env = dict(fixture["env"], CODEX_HOME=str(credentials))
    previous_files = set((credentials / "sessions").rglob("*.jsonl"))
    raw = bounded_process(argv, cwd=fixture["cwd"], env=env, timeout=args.timeout, stdin=prompt)
    (directory / "trace.jsonl").write_text(raw["stdout"])
    (directory / "stderr.txt").write_text(raw["stderr"])
    telemetry = parse_events(raw)
    telemetry["tool_ledger_records"] = capture_tool_ledger(credentials, previous_files, directory)
    result = {**cell, **telemetry, "returncode": raw["returncode"], "timed_out": raw["timed_out"],
              "elapsed_seconds": raw["elapsed_seconds"], "prompt_sha256": sha(prompt),
              "grade": grade(args, case, fixture, directory, credentials)}
    # Git output is data only; disable external diff/textconv and inherited hooks.
    diff = postrun_command(args, fixture, credentials,
                          ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", fixture["initial_head"]])
    (directory / "changes.diff").write_text(diff)
    (directory / "git-status.txt").write_text(postrun_command(args, fixture, credentials,
         ["git", "-c", "core.fsmonitor=false", "status", "--short"]))
    commits = postrun_command(args, fixture, credentials,
                             ["git", "rev-list", "--count", fixture["initial_head"] + "..HEAD"])
    result["new_commit_count"] = int(commits.strip())
    result["task_state"] = task_state(args, fixture, credentials)
    result["phase_boundary_respected"] = result["task_state"]["status"] == "In Progress" and result["task_state"]["session_count"] == 1 and result["task_state"]["open_session_count"] == 1
    result["workflow_checks"] = {"implementation_commit": result["new_commit_count"] > 0,
                                 "progress_recorded": result["task_state"]["progress_count"] > 0,
                                 "phase_boundary": result["phase_boundary_respected"]}
    result["correct"] = result["execution_ok"] and result["grade"]["correct"] and all(result["workflow_checks"].values())
    result["workflow_audit_required"] = "Manually verify agent-authored regression failed before fix, passed afterward, meaningful coverage, and actual delegation from raw trace. No automated red/green claim."
    save(directory / "result.json", result)
    return result


def summarize(results, plan):
    identity = lambda row: (row.get("case"), row.get("variant"), row.get("repetition"))
    expected = [identity(row) for row in plan]
    actual = [identity(row) for row in results]
    complete = len(expected) == len(set(expected)) and len(actual) == len(set(actual)) and set(expected) == set(actual)
    def correct(row):
        checks = row.get("grade", {}).get("checks", {})
        workflow = row.get("workflow_checks", {})
        return row.get("execution_ok") is True and row.get("grade", {}).get("correct") is True and bool(checks) and all(v is True for v in checks.values()) and row.get("phase_boundary_respected") is True and bool(workflow) and all(v is True for v in workflow.values())
    unique = [row for row in results if actual.count(identity(row)) == 1 and identity(row) in expected]
    passed = sum(correct(row) for row in unique)
    return {"planned": len(plan), "recorded": len(results),
            "complete_matrix": complete,
            "correct": passed,
            "failed_or_incomplete": len(plan) - passed,
            "total_tokens": None, "cost": None,
            "scope": "real historical implementation phase; coordinator owns setup and shipment",
            "variants": {v: {"attempts": sum(r["variant"] == v for r in results),
                              "correct": sum(r["variant"] == v and correct(r) for r in unique)} for v in VARIANTS}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--reasoning", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--variant", choices=VARIANTS, action="append", dest="variants")
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--auth-file", type=Path, default=Path.home() / ".codex/auth.json")
    parser.add_argument("--calibrate-only", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or not args.codex:
        parser.error("macOS and Codex CLI required; no unsandboxed fallback")
    if not args.calibrate_only and not args.model:
        parser.error("--model required for live attempts")
    if args.timeout <= 0 or args.repetitions < 1:
        parser.error("positive timeout and repetitions required")
    output = args.output.resolve()
    if not disposable_output(output):
        parser.error("output must be a fresh directory outside all Git checkouts")
    loaded = json.loads((HERE / "cases.json").read_text())
    cases = loaded["cases"] if isinstance(loaded, dict) else loaded
    if args.cases:
        if set(args.cases) - {c["id"] for c in cases}:
            parser.error("unknown case")
        cases = [c for c in cases if c["id"] in args.cases]
    variants = args.variants or list(VARIANTS)
    if len(variants) != len(set(variants)):
        parser.error("duplicate variants")
    env = clean_env(output)
    full = checked(["git", "show", PROMPT_REF + ":codex-prompts/tusk.md"], ROOT, env)
    recovery = checked(["git", "show", PROMPT_REF + ":codex-prompts/tusk-recovery.md"], ROOT, env)
    cores = {"full": full, "compact": (HERE / "compact.md").read_text()}
    plan = [{"case": c["id"], "variant": v, "repetition": rep}
            for rep in range(1, args.repetitions + 1) for index, c in enumerate(cases)
            for v in (variants if (rep + index) % 2 else list(reversed(variants)))]
    output.mkdir(parents=True)
    (output / "sources").mkdir()
    for name in ("runner.py", "grader.py", "cases.json", "compact.md"):
        shutil.copyfile(HERE / name, output / "sources" / name)
    save(output / "manifest.json", {"format_version": 1, "prompt_ref": PROMPT_REF, "cases": cases,
         "plan": plan, "model": args.model, "reasoning": args.reasoning, "timeout": args.timeout,
         "runner_sha256": sha(Path(__file__).read_bytes()), "grader_sha256": sha((HERE / "grader.py").read_bytes()),
         "case_sha256": sha((HERE / "cases.json").read_bytes()), "prompt_sha256": {v: sha(c) for v, c in cores.items()},
         "recovery_sha256": sha(recovery), "cli_version": checked([args.codex, "--version"], ROOT, env),
         "scope": "implementation/verification/commit only; not full task lifecycle",
         "fixture_adaptations": ["Remove automatic historical agent configuration.",
                                 "Use case-specific existing test suite as commit gate.",
                                 "Pass explicit TMPDIR via mktemp -p for two BSD -t calls in bin/tusk."],
         "limitations": ["Two known historical small bugs are calibration cases, not held-out validation.",
                          "Parent token usage does not establish child-inclusive totals.",
                          "Historical source skills/docs remain inspectable; selected workflow is authoritative."]})
    for variant, core in cores.items():
        (output / f"prompt-{variant}.md").write_text(core)
    (output / "tusk-recovery.md").write_text(recovery)
    results, calibration = [], []
    with tempfile.TemporaryDirectory(prefix="tusk-replay-auth-") as auth:
        credentials = Path(auth)
        credentials.chmod(0o700)
        if args.auth_file.is_file():
            shutil.copyfile(args.auth_file, credentials / "auth.json")
        elif not args.calibrate_only:
            parser.error("Codex auth file unavailable")
        else:
            (credentials / "auth.json").write_text("{}")
        (credentials / "auth.json").chmod(0o600)
        try:
            for case in cases:
                for label, ref in (("before", case["pre_ref"]), ("fixed", case["fixed_ref"])):
                    directory = output / "calibration" / case["id"] / label
                    fixture = make_fixture(case, directory, ref)
                    sandbox_preflight(args, fixture, credentials, directory)
                    evidence = grade(args, case, fixture, directory, credentials)
                    calibration.append({"case": case["id"], "revision": label, "ref": ref, "grade": evidence})
                    save(output / "calibration.json", calibration)
                    if evidence.get("error") or evidence["correct"] != (label == "fixed"):
                        raise RuntimeError("grader calibration failed; model calls refused")
                directory = output / "calibration" / case["id"] / "task-startup"
                fixture = make_fixture(case, directory, case["pre_ref"], task=True)
                sandbox_preflight(args, fixture, credentials, directory)
        except Exception as exc:
            results = [{**cell, "correct": False, "not_started": True,
                        "error": f"Calibration/preflight failure: {type(exc).__name__}: {exc}"} for cell in plan]
            save(output / "results.json", results)
            save(output / "summary.json", summarize(results, plan))
            print(json.dumps({"calibration_passed": False, "error": str(exc)}))
            return 1
        if args.calibrate_only:
            print(json.dumps({"calibration_passed": True, "cases": len(cases)}))
            return 0
        for cell in plan:
            case = next(c for c in cases if c["id"] == cell["case"])
            directory = output / f"{cell['repetition']:02d}-{cell['case']}-{cell['variant']}"
            try:
                result = run_attempt(args, case, cell, directory, cores[cell["variant"]], recovery, credentials)
            except Exception as exc:
                result = {**cell, "correct": False, "error": f"{type(exc).__name__}: {exc}"}
                save(directory / "result.json", result)
            results.append(result)
            save(output / "results.json", results)
            save(output / "summary.json", summarize(results, plan))
            print(json.dumps({**cell, "correct": result["correct"], "error": result.get("error")}), flush=True)
    return 0 if all(r.get("correct") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
