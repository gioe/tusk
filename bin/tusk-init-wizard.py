#!/usr/bin/env python3
"""Interactive tusk setup wizard, callable from any shell.

Ports the /tusk-init Claude Code skill into a CLI subcommand so Codex users
(and non-interactive automation) can configure tusk/config.json without
hand-editing. Safe to re-run on an existing project: the wizard updates
`tusk/config.json` and refreshes validation triggers via
`tusk regen-triggers`, but never touches task data — rows in `tasks`,
`acceptance_criteria`, `task_sessions`, and `skill_runs` are preserved
across runs (issue #604). Orchestrates the existing helper scripts:

  - tusk init-scan-codebase  → propose domains
  - tusk test-detect         → propose test_command
  - tusk init-write-config   → merge config, back up, refresh validation
                                triggers (non-destructive when DB exists)
  - tusk init-fetch-bootstrap → fetch project_libs bootstrap tasks
  - tusk task-insert         → seed tasks

Usage:
    tusk init-wizard [options]

Modes:
    --interactive       Force interactive prompts (default when stdin is a TTY).
    --non-interactive   Skip all prompts; use flags and scan results only
                        (default when stdin is not a TTY).

Config flags (pass only what you want to override — other keys carry forward):
    --domains <json_array>       JSON array of domain names
    --agents <json_object>       JSON object {agent_name: {...}}
    --task-types <json_array>    JSON array of task type names
    --test-command <string>      Test command (empty string clears)
    --project-type <string>      Project type key (empty string clears)
    --project-libs <json_object> JSON object {name: {repo, ref}}

Behaviour flags:
    --auto-scan / --no-auto-scan   Run init-scan-codebase + test-detect to
                                   derive defaults when flags are absent.
                                   Default: --auto-scan.
    --seed-bootstrap-tasks <mode>  none|all — whether to seed tasks from
                                   project_libs bootstrap. Default: none in
                                   non-interactive mode.

Scaffolding flags (mutually exclusive):
    --scaffold-spec <json>   JSON array of {name, purpose, agent} objects;
                             after writing config, invokes `tusk init-scaffold`
                             with the spec. Lets one-shot CI runs do the full
                             bootstrap (config + scaffolding + bootstrap tasks)
                             in a single init-wizard invocation.
    --no-scaffold            Explicit opt-out marker. Equivalent to omitting
                             --scaffold-spec; included for symmetry with the
                             /tusk-init SKILL.md flow.

Output (JSON):
    {
      "success": true,
      "mode": "non-interactive" | "interactive",
      "config_path": "/path/to/tusk/config.json",
      "written": {"domains": [...], "agents": {...}, ...},
      "scan": {"manifests": [...], "detected_domains": [...]},
      "scaffold": null | {"success": true, "mode": ..., "created": [...], "skipped": [...]},
      "seeded_tasks": [{"task_id": 42, "summary": "..."}],
      "skipped_tasks": [{"summary": "...", "reason": "..."}]
    }
    {
      "success": false,
      "error": "<reason>",
      ...partial progress under same keys as above...
    }
"""

import argparse
import json
import os
import subprocess
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TASK_TYPES = ["bug", "feature", "refactor", "test", "docs", "infrastructure"]

# Domain → agent name mapping (mirrors /tusk-init Step 4, SKILL.md lines 220–234).
_DOMAIN_TO_AGENT = {
    "frontend": "frontend",
    "api": "backend",
    "database": "backend",
    "infrastructure": "infrastructure",
    "docs": "docs",
    "mobile": "mobile",
    "data": "data",
    "ml": "data",
    "cli": "cli",
    "auth": "backend",
}

# Default descriptions for auto-derived agents. Config validator requires agents
# to be a dict of string→string, so the value must be a plain description string.
_AGENT_DESCRIPTIONS = {
    "frontend": "UI, components, and client-side code",
    "backend": "APIs, services, and database work",
    "infrastructure": "CI/CD, deployment, and infra-as-code",
    "docs": "Documentation and reference material",
    "mobile": "iOS, Android, and cross-platform mobile",
    "data": "Data pipelines, ML models, and analytics",
    "cli": "Command-line tools and scripts",
    "general": "Default catch-all agent",
}


def _emit(payload: dict) -> None:
    print(json.dumps(payload))


def _fail(msg: str, **extra) -> None:
    payload = {"success": False, "error": msg}
    payload.update(extra)
    _emit(payload)
    sys.exit(1)


def _run_tusk(args: list, timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke a nested tusk subcommand. Inherits TUSK_DB so tests pin correctly.
    Pass encoding='utf-8' so nested output with non-ASCII bytes (e.g. user's
    project-type name, test-command args, bootstrap task summaries) doesn't
    UnicodeDecodeError on non-UTF-8 locales."""
    return subprocess.run(
        ["tusk", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _parse_json_arg(name: str, raw: str, expected_type: type, type_label: str):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        _fail(f"--{name} is not valid JSON: {e}")
    if not isinstance(value, expected_type):
        _fail(f"--{name} must be a JSON {type_label}")
    return value


def _scan_codebase() -> dict:
    """Return init-scan-codebase output, or {manifests:[], detected_domains:[]} on error."""
    try:
        result = _run_tusk(["init-scan-codebase"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"manifests": [], "detected_domains": []}
    if result.returncode != 0 or not result.stdout.strip():
        return {"manifests": [], "detected_domains": []}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"manifests": [], "detected_domains": []}


def _detect_test_command() -> dict:
    """Return test-detect output, or {command: null, confidence: 'none'} on error."""
    try:
        result = _run_tusk(["test-detect"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"command": None, "confidence": "none"}
    if result.returncode != 0 or not result.stdout.strip():
        return {"command": None, "confidence": "none"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"command": None, "confidence": "none"}


def _derive_agents(domains: list) -> dict:
    """Map a list of domain names to an agents dict using the /tusk-init rules.

    Config validator requires string values, so each agent gets a description
    from _AGENT_DESCRIPTIONS (falling back to a generic label)."""
    agents: dict = {}
    for domain in domains:
        mapped = _DOMAIN_TO_AGENT.get(domain)
        if mapped and mapped not in agents:
            agents[mapped] = _AGENT_DESCRIPTIONS.get(mapped, mapped)
    if "general" not in agents:
        agents["general"] = _AGENT_DESCRIPTIONS["general"]
    return agents


def _prompt(question: str, default: str = "") -> str:
    """Prompt once; return stripped answer (default on empty). Returns default on EOF."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _interactive_collect(scan: dict, test_detect: dict, overrides: dict) -> dict:
    """Walk the user through confirming each config value. Returns a dict with
    the keys that should be passed to init-write-config (only those actually
    confirmed/entered; absent keys carry forward from existing config)."""
    picked: dict = {}

    print("Detected manifests:", ", ".join(scan.get("manifests") or []) or "(none)")
    proposed_domains = [d["name"] for d in scan.get("detected_domains") or []]
    if "domains" in overrides:
        picked["domains"] = overrides["domains"]
        print(f"Using --domains override: {picked['domains']}")
    else:
        default_str = ",".join(proposed_domains)
        raw = _prompt(
            "Domains (comma-separated, empty = no validation)",
            default=default_str,
        )
        picked["domains"] = [d.strip() for d in raw.split(",") if d.strip()]

    if "agents" in overrides:
        picked["agents"] = overrides["agents"]
        print(f"Using --agents override: {picked['agents']}")
    else:
        proposed_agents = _derive_agents(picked.get("domains") or proposed_domains)
        default_str = ",".join(sorted(proposed_agents.keys()))
        raw = _prompt(
            "Agents (comma-separated names, empty = no validation)",
            default=default_str,
        )
        names = [n.strip() for n in raw.split(",") if n.strip()]
        picked["agents"] = {
            n: _AGENT_DESCRIPTIONS.get(n, n) for n in names
        }

    if "task_types" in overrides:
        picked["task_types"] = overrides["task_types"]
    else:
        default_str = ",".join(DEFAULT_TASK_TYPES)
        raw = _prompt("Task types (comma-separated)", default=default_str)
        picked["task_types"] = [t.strip() for t in raw.split(",") if t.strip()]

    if "test_command" in overrides:
        picked["test_command"] = overrides["test_command"]
    else:
        default_cmd = test_detect.get("command") or ""
        answer = _prompt("Test command (empty to skip)", default=default_cmd)
        picked["test_command"] = answer

    if "project_type" in overrides:
        picked["project_type"] = overrides["project_type"]
    else:
        answer = _prompt(
            "Project type (e.g. ios_app, python_service; empty = none)",
            default="",
        )
        picked["project_type"] = answer

    if "project_libs" in overrides:
        picked["project_libs"] = overrides["project_libs"]

    return picked


def _non_interactive_collect(scan: dict, test_detect: dict, overrides: dict, auto_scan: bool) -> dict:
    """Build a picked dict without prompting. Flags win over scan-derived values."""
    picked: dict = {}

    if "domains" in overrides:
        picked["domains"] = overrides["domains"]
    elif auto_scan:
        picked["domains"] = [d["name"] for d in scan.get("detected_domains") or []]

    if "agents" in overrides:
        picked["agents"] = overrides["agents"]
    elif auto_scan:
        source_domains = picked.get("domains") or []
        picked["agents"] = _derive_agents(source_domains)

    if "task_types" in overrides:
        picked["task_types"] = overrides["task_types"]
    elif auto_scan:
        picked["task_types"] = list(DEFAULT_TASK_TYPES)

    if "test_command" in overrides:
        picked["test_command"] = overrides["test_command"]
    elif auto_scan and test_detect.get("command"):
        picked["test_command"] = test_detect["command"]

    if "project_type" in overrides:
        picked["project_type"] = overrides["project_type"]

    if "project_libs" in overrides:
        picked["project_libs"] = overrides["project_libs"]

    return picked


def _apply_write_config(picked: dict) -> dict:
    """Invoke tusk init-write-config with the picked values. Returns its JSON output
    (with success=False on any failure)."""
    cmd = ["init-write-config"]
    if "domains" in picked:
        cmd += ["--domains", json.dumps(picked["domains"])]
    if "agents" in picked:
        cmd += ["--agents", json.dumps(picked["agents"])]
    if "task_types" in picked:
        cmd += ["--task-types", json.dumps(picked["task_types"])]
    if "test_command" in picked:
        cmd += ["--test-command", picked["test_command"]]
    if "project_type" in picked:
        cmd += ["--project-type", picked["project_type"]]
    if "project_libs" in picked:
        cmd += ["--project-libs", json.dumps(picked["project_libs"])]

    try:
        result = _run_tusk(cmd, timeout=120)
    except FileNotFoundError:
        return {"success": False, "error": "tusk not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "init-write-config timed out"}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        stderr = (result.stderr or "").strip()
        return {"success": False, "error": stderr or "init-write-config produced no JSON output"}


def _run_scaffold(spec_json: str) -> dict:
    """Invoke `tusk init-scaffold --spec <json>` and return its parsed JSON output.
    Returns a {"success": False, "error": ...} dict on any failure (subprocess
    error, non-zero exit, or unparseable stdout) — matches the shape callers see
    on the success path."""
    try:
        result = _run_tusk(["init-scaffold", "--spec", spec_json], timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"success": False, "error": f"init-scaffold failed to run: {e}"}
    if not result.stdout.strip():
        stderr = (result.stderr or "").strip()
        return {"success": False, "error": stderr or "init-scaffold produced no JSON output"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        stderr = (result.stderr or "").strip()
        return {"success": False, "error": stderr or "init-scaffold produced invalid JSON"}


def _seed_bootstrap_tasks(interactive: bool) -> tuple:
    """Run init-fetch-bootstrap + task-insert for each bootstrap task.
    Returns (seeded_tasks, skipped_tasks)."""
    seeded: list = []
    skipped: list = []

    try:
        result = _run_tusk(["init-fetch-bootstrap"], timeout=90)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        skipped.append({"summary": "<bootstrap fetch>", "reason": f"fetch failed: {e}"})
        return seeded, skipped

    if result.returncode != 0 or not result.stdout.strip():
        err = (result.stderr or "").strip() or "init-fetch-bootstrap exited non-zero"
        skipped.append({"summary": "<bootstrap fetch>", "reason": err})
        return seeded, skipped

    try:
        bootstrap = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        skipped.append({"summary": "<bootstrap fetch>", "reason": f"JSON parse error: {e}"})
        return seeded, skipped

    for lib in bootstrap.get("libs") or []:
        lib_name = lib.get("name")
        if lib.get("error"):
            skipped.append({
                "summary": f"<lib: {lib_name}>",
                "reason": lib["error"],
            })
            continue
        for task in lib.get("tasks") or []:
            summary = task.get("summary", "").strip()
            if not summary:
                continue
            if interactive and not _prompt_yes_no(f"Seed '{summary}'?", default=True):
                skipped.append({"summary": summary, "reason": "user declined"})
                continue
            criteria = list(task.get("criteria") or [])
            for hint in task.get("migration_hints") or []:
                criteria.append(f"[Migration] {hint}")
            insert_cmd = [
                "task-insert",
                summary,
                task.get("description", ""),
                "--priority", task.get("priority", "Medium"),
                "--task-type", task.get("task_type", "feature"),
                "--complexity", task.get("complexity", "M"),
            ]
            for c in criteria:
                insert_cmd += ["--criteria", c]
            try:
                ir = _run_tusk(insert_cmd, timeout=60)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                skipped.append({"summary": summary, "reason": f"task-insert failed: {e}"})
                continue
            try:
                body = json.loads(ir.stdout) if ir.stdout.strip() else {}
            except json.JSONDecodeError:
                body = {}
            if ir.returncode == 0 and body.get("task_id"):
                seeded.append({"task_id": body["task_id"], "summary": summary})
            elif body.get("duplicate"):
                skipped.append({
                    "summary": summary,
                    "reason": f"duplicate of TASK-{body.get('matched_task_id')}",
                })
            else:
                err = (ir.stderr or body.get("error") or "").strip() or f"exit {ir.returncode}"
                skipped.append({"summary": summary, "reason": err})

    return seeded, skipped


def main():
    if len(sys.argv) < 3:
        _fail(
            "tusk-init-wizard.py requires <db_path> and <config_path> as the "
            "first two positional arguments (invoked via `tusk init-wizard`)."
        )

    # Print help and exit before any side effects. parse_known_args silently
    # drops --help when add_help=False, so the wizard would otherwise run and
    # rewrite tusk/config.json on `tusk init-wizard --help`.
    forwarded = sys.argv[3:]
    if any(arg in ("--help", "-h") for arg in forwarded):
        print(__doc__)
        sys.exit(0)

    config_path = sys.argv[2]

    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interactive", action="store_true")
    mode.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--domains", default=None)
    parser.add_argument("--agents", default=None)
    parser.add_argument("--task-types", default=None, dest="task_types")
    parser.add_argument("--test-command", default=None, dest="test_command")
    parser.add_argument("--project-type", default=None, dest="project_type")
    parser.add_argument("--project-libs", default=None, dest="project_libs")
    parser.add_argument("--auto-scan", dest="auto_scan", action="store_true", default=True)
    parser.add_argument("--no-auto-scan", dest="auto_scan", action="store_false")
    parser.add_argument(
        "--seed-bootstrap-tasks",
        dest="seed_bootstrap",
        choices=["none", "all"],
        default=None,
    )
    parser.add_argument("--scaffold-spec", default=None, dest="scaffold_spec")
    parser.add_argument("--no-scaffold", action="store_true", dest="no_scaffold")
    args, _ = parser.parse_known_args(sys.argv[3:])

    # Resolve mode. --interactive / --non-interactive win; otherwise TTY-detect.
    if args.interactive:
        interactive = True
    elif args.non_interactive:
        interactive = False
    else:
        interactive = sys.stdin.isatty()

    # Parse JSON-valued overrides up front so we fail fast.
    overrides: dict = {}
    if args.domains is not None:
        overrides["domains"] = _parse_json_arg("domains", args.domains, list, "array")
    if args.agents is not None:
        overrides["agents"] = _parse_json_arg("agents", args.agents, dict, "object")
    if args.task_types is not None:
        overrides["task_types"] = _parse_json_arg("task-types", args.task_types, list, "array")
    if args.test_command is not None:
        overrides["test_command"] = args.test_command
    if args.project_type is not None:
        overrides["project_type"] = args.project_type
    if args.project_libs is not None:
        overrides["project_libs"] = _parse_json_arg(
            "project-libs", args.project_libs, dict, "object"
        )

    if args.scaffold_spec is not None and args.no_scaffold:
        _fail("--scaffold-spec and --no-scaffold are mutually exclusive")
    if args.scaffold_spec is not None:
        _parse_json_arg("scaffold-spec", args.scaffold_spec, list, "array")

    seed_bootstrap = args.seed_bootstrap
    if seed_bootstrap is None:
        seed_bootstrap = "all" if interactive else "none"

    scan = _scan_codebase() if args.auto_scan else {"manifests": [], "detected_domains": []}
    test_detect = _detect_test_command() if args.auto_scan else {"command": None, "confidence": "none"}

    if interactive:
        picked = _interactive_collect(scan, test_detect, overrides)
    else:
        picked = _non_interactive_collect(scan, test_detect, overrides, args.auto_scan)

    write_result = _apply_write_config(picked)
    if not write_result.get("success"):
        _emit({
            "success": False,
            "mode": "interactive" if interactive else "non-interactive",
            "config_path": config_path,
            "error": write_result.get("error") or "init-write-config failed",
            "written": picked,
            "scan": scan,
            "scaffold": None,
            "seeded_tasks": [],
            "skipped_tasks": [],
        })
        sys.exit(1)

    scaffold_result = _run_scaffold(args.scaffold_spec) if args.scaffold_spec is not None else None

    seeded: list = []
    skipped: list = []
    if seed_bootstrap == "all":
        seeded, skipped = _seed_bootstrap_tasks(interactive)

    _emit({
        "success": True,
        "mode": "interactive" if interactive else "non-interactive",
        "config_path": write_result.get("config_path", config_path),
        "written": picked,
        "scan": scan,
        "scaffold": scaffold_result,
        "seeded_tasks": seeded,
        "skipped_tasks": skipped,
    })


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-wizard [options]", file=sys.stderr)
        sys.exit(1)
    main()
