"""Verify that bin/tusk's empty-arg Usage message is built dynamically from
the dispatcher case-arms — i.e. invoking `tusk` with no args produces a Usage
line whose subcommand list exactly matches (in content and order) the
dispatcher case-arm names parsed from the source.

This is the runtime counterpart to Rule 25's static check: Rule 25 flags drift
when Usage is hardcoded; this test guarantees that the dynamic Usage cannot
drift in the first place, on every install where bin/tusk runs.
"""

import os
import re
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_PATH = os.path.join(REPO_ROOT, "bin", "tusk")


def _dispatcher_subcommands_from_source() -> list[str]:
    with open(TUSK_PATH, encoding="utf-8") as f:
        lines = f.readlines()

    start = end = None
    for i, line in enumerate(lines):
        if start is None and re.match(r'^case "\$\{1:-\}" in\b', line):
            start = i
            continue
        if start is not None and re.match(r"^esac\b", line):
            end = i
            break
    assert start is not None and end is not None, "dispatcher case block not found in bin/tusk"

    names = []
    for line in lines[start + 1 : end]:
        m = re.match(r"^  ([a-z][a-z0-9-]*)\)", line)
        if m:
            names.append(m.group(1))
    return names


def _runtime_usage_subcommands() -> list[str]:
    result = subprocess.run(
        [TUSK_PATH],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
    )
    assert result.returncode == 1, f"expected exit 1 from `tusk` with no args, got {result.returncode}"
    m = re.search(r"Usage: tusk \{([^}]*)\}", result.stderr)
    assert m, f"runtime Usage line not found in stderr:\n{result.stderr}"
    tokens = []
    for token in m.group(1).split("|"):
        token = token.strip().strip("\\").strip('"').strip()
        if re.fullmatch(r"[a-z][a-z0-9-]*", token):
            tokens.append(token)
    return tokens


def test_runtime_usage_matches_dispatcher_arms():
    dispatcher = _dispatcher_subcommands_from_source()
    runtime = _runtime_usage_subcommands()
    assert runtime == dispatcher, (
        "runtime Usage subcommand list does not match dispatcher case-arms\n"
        f"  in dispatcher only: {sorted(set(dispatcher) - set(runtime))}\n"
        f"  in runtime only:    {sorted(set(runtime) - set(dispatcher))}"
    )


def test_runtime_usage_includes_sql_catchall_marker():
    result = subprocess.run(
        [TUSK_PATH],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
    )
    assert '"SQL ..."' in result.stderr, (
        'runtime Usage must still advertise the catch-all "SQL ..." token '
        f"(distinct from dispatcher case-arms):\n{result.stderr}"
    )
