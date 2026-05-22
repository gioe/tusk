"""Integration tests for TUSK_TRACE=1 verbose-mode env var (issue #800).

`TUSK_TRACE=1` turns on `set -x` shell tracing in `bin/tusk` so operators
can reproduce silent failures with a line-level transcript in one step.
The trace is gated after the silent-exit guard so the trace output goes
to the operator's stderr (not the guard's captured inner-stderr buffer).
`TUSK_TRACE_ACTIVE=1` is exported so nested `tusk` invocations and
Python helpers can opt into matching verbose modes.
"""

import os
import subprocess


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _tusk(args, *, env):
    return subprocess.run(
        [TUSK_BIN, *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
    )


def _env(extra=None):
    base = os.environ.copy()
    base["TUSK_QUIET"] = "1"
    if extra:
        base.update(extra)
    return base


def test_trace_emits_set_x_lines_to_stderr():
    """With TUSK_TRACE=1, bin/tusk's shell trace appears on stderr."""
    result = _tusk(["version"], env=_env({"TUSK_TRACE": "1"}))

    assert result.returncode == 0, f"stderr: {result.stderr}"
    # set -x prefixes lines with `+ ` (or `++ ` for subshell-nested).
    trace_lines = [line for line in result.stderr.splitlines() if line.startswith(("+ ", "++ "))]
    assert trace_lines, (
        f"expected `+ ` set -x output on stderr; got stderr:\n{result.stderr}"
    )


def test_trace_silent_without_env_var():
    """Without TUSK_TRACE, no `+ ` trace lines appear."""
    env = _env()
    env.pop("TUSK_TRACE", None)
    result = _tusk(["version"], env=env)

    assert result.returncode == 0
    trace_lines = [line for line in result.stderr.splitlines() if line.startswith(("+ ", "++ "))]
    assert not trace_lines, (
        f"unexpected `+ ` trace output without TUSK_TRACE; stderr:\n{result.stderr}"
    )


def test_trace_silent_with_explicit_zero():
    """TUSK_TRACE=0 is treated as off, just like the variable being unset."""
    result = _tusk(["version"], env=_env({"TUSK_TRACE": "0"}))

    assert result.returncode == 0
    trace_lines = [line for line in result.stderr.splitlines() if line.startswith(("+ ", "++ "))]
    assert not trace_lines, f"TUSK_TRACE=0 should not enable trace; stderr:\n{result.stderr}"


def test_trace_exports_tusk_trace_active():
    """TUSK_TRACE=1 must export TUSK_TRACE_ACTIVE=1 so child processes can opt in.

    Run `printenv` via a `bash -c` subshell invoked by tusk itself wouldn't
    work without modifying tusk. Instead, verify the export by checking
    that the trace output includes the `TUSK_TRACE_ACTIVE=1` assignment line
    (set -x echoes every assignment).
    """
    result = _tusk(["version"], env=_env({"TUSK_TRACE": "1"}))

    assert result.returncode == 0
    assert "TUSK_TRACE_ACTIVE=1" in result.stderr, (
        f"expected TUSK_TRACE_ACTIVE=1 export to appear in trace; stderr:\n{result.stderr}"
    )


def test_trace_does_not_leak_into_silent_exit_guard():
    """Trace must be enabled AFTER the silent-exit guard so its output
    doesn't get captured into the guard's inner-stderr buffer and then
    re-printed (which would duplicate every line)."""
    result = _tusk(["version"], env=_env({"TUSK_TRACE": "1"}))

    assert result.returncode == 0
    # The recursion guard runs only when stderr is not a TTY. Since this
    # test pipes stderr, the guard activates — count occurrences of the
    # `set -x` echo of the trace activation block. It must appear exactly
    # once across stderr (from the guard's inner invocation), not twice.
    activation_lines = [
        line for line in result.stderr.splitlines()
        if "export TUSK_TRACE_ACTIVE=1" in line
    ]
    assert len(activation_lines) <= 1, (
        f"trace activation duplicated; would imply guard captured trace output. "
        f"saw {len(activation_lines)} lines:\n{activation_lines}"
    )
