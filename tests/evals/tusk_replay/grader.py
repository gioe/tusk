#!/usr/bin/env python3
"""Independent held-out behavioral checks; run only after the agent exits.

Usage: python -B grader.py CASE_ID REPO
Each invocation imports the candidate in a fresh process. These checks never
execute candidate-authored tests and do not use Python assertions.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def load(repo, filename):
    spec = importlib.util.spec_from_file_location("replay_candidate", repo / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def put(root, relative, content="keep me\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def inline_python(repo, scratch):
    module = load(repo, "tusk-task-brief.py")
    put(scratch, "services/report/schema.json", "{}\n")
    put(scratch, "services/report/check.py", "pass\n")
    checks = {}

    def warnings(command):
        return module._stale_spec_warnings(
            str(scratch), [{"id": 317, "verification_spec": command}]
        )

    # Existing literals permit both opaque-source and literal-aware scanners.
    source = shlex.quote('from pathlib import Path; print(Path("services/report/schema.json").exists())')
    for index, interpreter in enumerate(("python", "python3", "python3.11", "/usr/bin/python3")):
        for flag_index, flags in enumerate(("", "-I -B", "-W default -X dev")):
            prefix = f"{interpreter} {flags} -c {source}"
            checks[f"source_no_false_warning_{index}_{flag_index}"] = warnings(prefix) == []
            command = prefix + " absent/parameter.csv && cat absent/followup.json"
            result = warnings(command)
            missing = [p for row in result for p in row.get("details", {}).get("missing_paths", [])]
            checks[f"real_missing_operands_{index}_{flag_index}"] = (
                set(missing) == {"absent/parameter.csv", "absent/followup.json"}
                and all(row.get("details", {}).get("criterion_id") == 317 for row in result)
            )
            paths = module._spec_paths(command)
            checks[f"path_scan_retains_operands_{index}_{flag_index}"] = (
                "absent/parameter.csv" in paths and "absent/followup.json" in paths
            )
    checks["existing_trailing_and_chained_operands"] = warnings(
        f"python3 -B -c {source} services/report/schema.json; cat services/report/check.py"
    ) == []
    checks["normal_script_operand"] = "absent/run.py" in module._spec_paths("python3 absent/run.py")
    checks["ordinary_command_c_flag"] = "absent/pattern.txt" in module._spec_paths("rg -c absent/pattern.txt")
    return checks


def nested_cache(repo, scratch):
    module = load(repo, "tusk-merge.py")
    workspace = scratch / "tree"
    caches = [
        "__pycache__/root.pyc", ".pytest_cache/v/cache/nodeids",
        "components/worker/lib/__pycache__/module.pyc",
        "components/worker/.pytest_cache/v/cache/lastfailed",
        "tools/deep/jobs/__pycache__/job.pyc",
    ]
    for path in caches:
        put(workspace, path)
    protected = [
        "notes.txt", "components/worker/notes.txt",
        "components/__pycache__backup/archive.pyc",
        "tools/.pytest_cache_old/v/cache/state",
        "components/.git/__pycache__/metadata",
    ]
    for path in protected:
        put(workspace, path)
    outside = scratch / "outside"
    put(outside, "__pycache__/private.pyc")
    put(outside, "records.txt")
    (workspace / "linked_parent").symlink_to(outside, target_is_directory=True)
    (workspace / "components" / "__pycache__").symlink_to(outside, target_is_directory=True)
    (workspace / "tools" / ".pytest_cache").symlink_to(outside, target_is_directory=True)
    module._clean_generated_test_caches(str(workspace))
    checks = {
        "root_and_nested_caches_removed": all(not (workspace / path).exists() for path in caches),
        "unrelated_and_similarly_named_content_preserved": all(
            (workspace / path).is_file() and (workspace / path).read_text() == "keep me\n"
            for path in protected
        ),
        "external_targets_preserved": (outside / "__pycache__/private.pyc").read_text() == "keep me\n"
        and (outside / "records.txt").read_text() == "keep me\n",
        "symlink_caches_and_parents_preserved": all(
            (workspace / path).is_symlink()
            for path in ("linked_parent", "components/__pycache__", "tools/.pytest_cache")
        ),
    }

    # Exercise actual Git removal, using a disposable repository with no remotes.
    primary = scratch / "primary"
    primary.mkdir()
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("GIT_"):
            environment.pop(key)
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    def git(*args, required=True):
        result = subprocess.run(
            ["git", "-C", str(primary), *args], env=environment,
            capture_output=True, text=True, timeout=30,
        )
        if required and result.returncode:
            raise RuntimeError(f"Git fixture failed: {args!r}: {result.stderr}")
        return result

    git("init", "-q")
    put(primary, "project/package/module.py", "VALUE = 1\n")
    git("add", ".")
    git("-c", "user.name=Replay evaluator", "-c", "user.email=replay@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    clean = scratch / "cache_only"
    git("worktree", "add", "--detach", str(clean))
    put(clean, "project/package/__pycache__/module.pyc")
    put(clean, "project/.pytest_cache/v/cache/nodeids")
    module._clean_generated_test_caches(str(clean))
    checks["cache_only_real_worktree_removable"] = git("worktree", "remove", str(clean), required=False).returncode == 0
    dirty = scratch / "unknown_leftovers"
    git("worktree", "add", "--detach", str(dirty))
    put(dirty, "project/package/__pycache__/module.pyc")
    note = put(dirty, "project/operator-notes.txt", "irreplaceable\n")
    module._clean_generated_test_caches(str(dirty))
    checks["unknown_leftovers_block_real_removal"] = (
        git("worktree", "remove", str(dirty), required=False).returncode != 0
        and note.is_file() and note.read_text() == "irreplaceable\n"
    )
    return checks


def main():
    checks = {}
    diagnostic = None
    captured = io.StringIO()
    try:
        case_id, raw_repo = sys.argv[1:]
        repo = Path(raw_repo).resolve(strict=True)
        function = {"inline-python": inline_python, "nested-cache": nested_cache}[case_id]
        with tempfile.TemporaryDirectory(prefix="tusk-replay-grade-") as temporary:
            with contextlib.redirect_stdout(captured):
                checks = function(repo, Path(temporary))
    except Exception as exc:
        checks["grader_executed"] = False
        diagnostic = f"{type(exc).__name__}: {exc}"
    result = {"checks": checks, "correct": bool(checks) and all(value is True for value in checks.values())}
    if diagnostic:
        result["diagnostic"] = diagnostic
    if captured.getvalue():
        result["candidate_stdout"] = captured.getvalue()[-4000:]
    print(json.dumps(result, sort_keys=True))
    return 0 if result["correct"] else 1


if __name__ == "__main__":
    sys.exit(main())
