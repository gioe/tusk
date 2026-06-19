#!/usr/bin/env python3
"""Helper subcommands for the /address-issue skill.

Subcommands:
    classify-spec   Classify a Step 4.1 failing-test spec and recommend a
                    downstream action (store / null / discard) plus a
                    test_present score (yes / unverifiable / no).

`classify-spec` centralises five chunks of logic that were previously a
multi-branch decision tree expressed as SKILL.md prose:

  1. Effective first-token resolution (peeling `bash -c` / `sh -c` wrappers).
  2. Issue #589 short-circuit for `/`-containing tokens (bypassing `command -v`'s
     cwd-relative resolution).
  3. `command -v` PATH check on the sandbox PATH (/usr/bin:/bin).
  4. Sandbox-result classification: malformed / environmental / interpreter-
     wrapper-bypass / pass-through.
  5. The recommended downstream action for the calling skill.

Usage:

    # Pre-flight: returns the fast-path skip when the effective first token is
    # off the sandbox PATH; exits 2 ("sandbox required") otherwise.
    tusk address-issue classify-spec --spec-file spec.txt
    tusk address-issue classify-spec --spec 'pytest -q'
    printf '%s' "$SPEC" | tusk address-issue classify-spec

    # Post-sandbox: returns the final classification.
    tusk address-issue classify-spec \\
        --spec-file spec.txt \\
        --sandbox-exit 127 --sandbox-stderr-file stderr.txt

    # Implementer's choice for the exit-zero case.
    tusk address-issue classify-spec \\
        --spec '...' --sandbox-exit 0 --sandbox-stderr '' \\
        --exit-zero-decision keep   # or discard

Output (single-line JSON on stdout):

    {
      "action": "store" | "null" | "discard",
      "test_present": "yes" | "no" | "unverifiable",
      "reason": "<one-line classification rationale>",
      "effective_first_token": "<resolved token after wrapper peel>",
      "on_path": true | false
    }

Mapping back to the skill:
  - `store`   → store the spec as a typed `test` criterion in Step 6;
                test_present scores `"yes"`.
  - `null`    → `test_spec=null`; test_present scores `"unverifiable"`
                (concrete reproducer supplied but unvalidated).
  - `discard` → `test_spec=null`; test_present scores `"no"` (no reproducer
                or one that does not actually fail).

Exit codes:
  0  classification emitted on stdout
  2  pre-flight only and effective first token resolves on the sandbox PATH —
     caller must run the Step 4.1.c sandbox snippet and re-invoke with
     --sandbox-exit and --sandbox-stderr-file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

# Sandbox PATH used by the Step 4.1.c sandbox (env -i ... PATH=/usr/bin:/bin).
# The on-PATH check uses this verbatim so the helper's resolution exactly
# matches what the sandbox itself would resolve.
SANDBOX_PATH = "/usr/bin:/bin"


# ─── Effective first-token resolution ───────────────────────────────────────


def _nth_non_comment_token(text: str, pos: int) -> str:
    """Return the n-th whitespace-delimited token, skipping `#`-comment lines."""
    seen = 0
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for tok in line.split():
            seen += 1
            if seen == pos:
                return tok
    return ""


def resolve_effective_first_token(spec: str) -> str:
    """Peel `bash -c '<body>'` / `sh -c '<body>'` wrappers and return the
    spec's effective first token. Mirrors the awk pipeline at SKILL.md
    Step 4.1.a.

    For a wrapper, the body is the third positional arg (typically quoted);
    matching outer single or double quotes are stripped, then the body's first
    non-comment token is returned. For a non-wrapped spec, the literal first
    non-comment token is returned.
    """
    spec = spec or ""
    first = _nth_non_comment_token(spec, 1)
    second = _nth_non_comment_token(spec, 2)
    if first in ("bash", "sh") and second == "-c":
        rest = spec.split(None, 2)
        if len(rest) >= 3:
            body = rest[2].lstrip()
            if len(body) >= 2 and body[0] in ("'", '"') and body[-1] == body[0]:
                body = body[1:-1]
            return _nth_non_comment_token(body, 1)
    return first


def is_on_sandbox_path(token: str) -> bool:
    """True iff `token` resolves on PATH=/usr/bin:/bin.

    Tokens containing `/` short-circuit to False without consulting
    `shutil.which`. Issue #589: `command -v` (and `shutil.which`) special-case
    `/`-containing names by checking the file relative to cwd, bypassing PATH
    entirely. For relative paths like `bin/tusk` the orchestrator's cwd would
    resolve them — but the sandbox tempdir cannot reach them, so the on-PATH
    check must report False.
    """
    if not token:
        return False
    if "/" in token:
        return False
    return shutil.which(token, path=SANDBOX_PATH) is not None


# ─── Sandbox-stderr signature recognisers ───────────────────────────────────

# POSIX text utilities that handle a missing input file *internally* and exit
# 1 or 2 with a "<tool>: <path>: No such file or directory" stderr line. These
# do NOT exit 126/127, so the historic environmental branch (which gated on
# 126/127 only) misses them — the issue #659 gap. The text-tool environmental
# branch below catches them by stderr signature regardless of exit code.
TEXT_TOOLS = (
    "grep", "egrep", "fgrep", "rgrep",
    "awk", "gawk", "mawk", "nawk",
    "sed", "find", "cat", "ls",
    "head", "tail", "sort", "uniq",
    "cut", "tr", "wc", "file", "stat", "diff",
)

_TEXT_TOOL_NSFOD_RE = re.compile(
    # Tool-anchored signature: `<tool>:` at line start, then any prose, then a
    # `No such file or directory` substring. The interior is intentionally
    # loose — different implementations vary widely between this anchor and
    # the NSFOD tail (BSD `awk: No such file or directory`, GNU `gawk: cannot
    # open file '...' for reading: No such file or directory`, `grep: <path>:
    # No such file or directory`, etc.).
    r"^(?P<tool>(?:" + "|".join(re.escape(t) for t in TEXT_TOOLS) + r"))"
    r":.*No such file or directory",
    re.MULTILINE,
)

# Interpreter-wrapper-bypass signatures (Step 4.1.c). Order matters: the
# Python `-m` form is checked before the generic Python FileNotFoundError so
# the module-name path takes precedence for `python3 -m <module>` invocations.
_PYTHON_DASH_M_RE = re.compile(
    r"^.*?[Pp]ython[0-9.]*: No module named (?P<token>\S+)",
    re.MULTILINE,
)
_PYTHON_FNFE_RE = re.compile(
    r"FileNotFoundError: \[Errno 2\] No such file or directory: ['\"](?P<token>[^'\"]+)['\"]"
)
_NODE_SPAWN_ENOENT_RE = re.compile(
    r"spawn (?P<token>\S+) ENOENT"
)
_NODE_TRAILING_ENOENT_RE = re.compile(
    r"(?:^|\s)(?P<token>[^\s/]+) ENOENT\s*$",
    re.MULTILINE,
)
_RUBY_ENOENT_RE = re.compile(
    r"Errno::ENOENT: No such file or directory[^\n]*?-\s*(?P<token>\S+)"
)
_PERL_EXEC_RE = re.compile(
    r"Can't exec [\"'](?P<token>[^\"']+)[\"']: No such file or directory"
)
# Generic last-resort: a bare `<token>: No such file or directory` line where
# <token> has no path component. Tightly anchored to avoid false positives on
# the Python/Ruby/Perl signatures above (which are checked first).
_GENERIC_NSFOD_RE = re.compile(
    r"^(?P<token>[A-Za-z0-9_.+-]+): No such file or directory\s*$",
    re.MULTILINE,
)


def _extract_wrapper_match(stderr: str):
    """Return (lang_label, token, is_python_module) for an interpreter-
    wrapper-bypass match in `stderr`, else None.

    `is_python_module` is True only for the Python `-m` form — its token is a
    module name, not an executable, so the caller must skip the on-PATH check
    and route directly to "unverifiable".
    """
    m = _PYTHON_DASH_M_RE.search(stderr)
    if m:
        return ("python -m", m.group("token"), True)
    m = _PYTHON_FNFE_RE.search(stderr)
    if m:
        return ("python", m.group("token"), False)
    m = _NODE_SPAWN_ENOENT_RE.search(stderr)
    if m:
        return ("node", m.group("token"), False)
    m = _NODE_TRAILING_ENOENT_RE.search(stderr)
    if m:
        return ("node", m.group("token"), False)
    m = _RUBY_ENOENT_RE.search(stderr)
    if m:
        return ("ruby", m.group("token"), False)
    m = _PERL_EXEC_RE.search(stderr)
    if m:
        return ("perl", m.group("token"), False)
    m = _GENERIC_NSFOD_RE.search(stderr)
    if m:
        return ("generic", m.group("token"), False)
    return None


# `command not found` token recognisers. The bash sandbox emits
# `bash: <token>: command not found` (optionally with a `line N:` segment);
# some shells emit the token after the phrase (`zsh: command not found: <token>`).
# The "after" form is checked first so the zsh ordering does not mis-capture
# the shell name from the "before" pattern.
_CNF_TOKEN_AFTER_RE = re.compile(
    r"command not found:\s*(?P<token>\S+)", re.IGNORECASE
)
_CNF_TOKEN_BEFORE_RE = re.compile(
    r"(?P<token>[^\s:]+):\s*command not found", re.IGNORECASE
)


def _extract_command_not_found_token(stderr: str):
    """Return the missing command name from a `command not found` stderr line,
    or None when no such line is present.

    Handles the common shell phrasings:
      bash: tusk: command not found
      bash: line 2: tusk: command not found   -> tusk
      tusk: command not found                  -> tusk
      zsh: command not found: tusk             -> tusk
    """
    if not stderr:
        return None
    m = _CNF_TOKEN_AFTER_RE.search(stderr)
    if m:
        return m.group("token")
    m = _CNF_TOKEN_BEFORE_RE.search(stderr)
    if m:
        return m.group("token")
    return None


def _first_line(text: str) -> str:
    if not text:
        return "(empty stderr)"
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "(empty stderr)"


# ─── Post-sandbox classification ────────────────────────────────────────────


def classify_post_sandbox(
    sandbox_exit: int,
    sandbox_stderr: str,
    exit_zero_decision: str | None,
    effective_first_token: str = "",
) -> dict:
    """Return {action, test_present, reason} given sandbox results. The
    caller layers `effective_first_token` and `on_path` onto the dict before
    emitting it.

    `effective_first_token` is the spec's resolved first command (the same value
    the caller layers onto the result). It is consumed by the `command not found`
    branch to tell a genuinely malformed spec (the spec's own command is
    missing) apart from the issue #1114 environmental case (a downstream project
    tool the sandbox stripped from PATH).

    Branch order is significant: language-specific interpreter-wrapper-bypass
    signatures are checked before the generic `<tool>: ... No such file or
    directory` environmental rule, because Python's FileNotFoundError, Ruby's
    Errno::ENOENT, and Perl's exec error all contain "No such file or
    directory" verbatim and would otherwise be misrouted to the environmental
    branch (correct end action, but a less precise reason field).
    """
    stderr = sandbox_stderr or ""

    # Exit 0 — spec passed before any fix. Implementer chooses keep vs discard.
    if sandbox_exit == 0:
        if exit_zero_decision == "keep":
            return {
                "action": "null",
                "test_present": "unverifiable",
                "reason": (
                    "exit 0: implementer chose keep; spec attempted but did "
                    "not reach validation logic (self-skip guard fired in sandbox)"
                ),
            }
        # default + "discard": treat as if no failing test was supplied.
        return {
            "action": "discard",
            "test_present": "no",
            "reason": (
                "exit 0: spec passed before any fix; implementer chose discard"
                if exit_zero_decision == "discard"
                else "exit 0: spec passed before any fix; no implementer decision provided (defaulting to discard)"
            ),
        }

    lower = stderr.lower()

    # Syntax error — an unambiguously broken spec regardless of exit code or
    # which token names it.
    if "syntax error" in lower:
        return {
            "action": "discard",
            "test_present": "no",
            "reason": f"malformed spec: {_first_line(stderr)}",
        }

    # `command not found` — malformed ONLY when the missing token is the spec's
    # own effective first command. When it names a downstream tool that the
    # sandbox stripped from PATH (issue #1114 — e.g. a project `tusk`/`pytest`
    # invoked after an on-PATH command like `cp`), the sandbox merely removed a
    # project binary, exactly like the Step 4.1.a fast-path skip: environmental,
    # not malformed. A "command not found" whose token cannot be parsed falls
    # back to malformed (preserving the historic behaviour).
    if "command not found" in lower:
        cnf_token = _extract_command_not_found_token(stderr)
        if (
            cnf_token
            and cnf_token != effective_first_token
            and not is_on_sandbox_path(cnf_token)
        ):
            return {
                "action": "null",
                "test_present": "unverifiable",
                "reason": (
                    f"environmental: 'command not found' names downstream tool "
                    f"'{cnf_token}' off sandbox PATH, not the effective first "
                    f"token '{effective_first_token}'"
                ),
            }
        return {
            "action": "discard",
            "test_present": "no",
            "reason": f"malformed spec: {_first_line(stderr)}",
        }

    # Exit 126/127: classic environmental signature (empty stderr or NSFOD).
    if sandbox_exit in (126, 127):
        if not stderr.strip() or "No such file or directory" in stderr:
            return {
                "action": "null",
                "test_present": "unverifiable",
                "reason": (
                    f"environmental: exit {sandbox_exit} with sandbox unreachable-path "
                    f"signature ({_first_line(stderr)})"
                ),
            }
        # 126/127 with stderr that matches neither environmental nor malformed
        # signatures — Step 4.1.c routes this to malformed.
        return {
            "action": "discard",
            "test_present": "no",
            "reason": (
                f"malformed spec: exit {sandbox_exit} with unrecognized stderr "
                f"({_first_line(stderr)})"
            ),
        }

    # Interpreter-wrapper-bypass: language runtime ran cleanly on /usr/bin:/bin
    # but its inner subprocess could not reach the project tool. Checked before
    # the text-tool environmental rule because Python/Ruby/Perl signatures
    # contain "No such file or directory" verbatim and would otherwise misroute.
    wrap = _extract_wrapper_match(stderr)
    if wrap is not None:
        lang, token, is_module = wrap
        if is_module:
            return {
                "action": "null",
                "test_present": "unverifiable",
                "reason": (
                    f"interpreter-wrapper bypass ({lang}): inner subprocess "
                    f"could not import module '{token}' (sandbox env -i "
                    f"strips Python site-packages)"
                ),
            }
        basename = os.path.basename(token)
        if not is_on_sandbox_path(basename):
            return {
                "action": "null",
                "test_present": "unverifiable",
                "reason": (
                    f"interpreter-wrapper bypass ({lang}): inner subprocess "
                    f"could not reach '{token}' on sandbox PATH"
                ),
            }
        # Token IS on /usr/bin:/bin: a system tool genuinely failed inside the
        # wrapper. Fall through to the pass-through "real failure" branch.

    # Issue #659: text-tool environmental — exit nonzero (any code) with a
    # `<tool>: <path>: No such file or directory` stderr line, where <tool> is
    # a known POSIX text utility. These tools handle missing inputs internally
    # and exit 1 or 2, so the historic exit-126/127 environmental rule misses
    # them. The fix preserves the spec author's intent: a polarity-correct
    # grep/awk/sed against project-relative paths is unverifiable in the
    # sandbox tempdir, not malformed and not a real failure.
    m = _TEXT_TOOL_NSFOD_RE.search(stderr)
    if m:
        return {
            "action": "null",
            "test_present": "unverifiable",
            "reason": (
                f"environmental: exit {sandbox_exit} from {m.group('tool')} "
                f"with No such file or directory signature ({_first_line(stderr)})"
            ),
        }

    # Pass-through: nonzero exit with no recognised command-error signature.
    return {
        "action": "store",
        "test_present": "yes",
        "reason": f"real failure: spec exited {sandbox_exit}",
    }


# ─── CLI ────────────────────────────────────────────────────────────────────


def _resolve_spec(args) -> str:
    if args.spec is not None:
        return args.spec
    if args.spec_file:
        if args.spec_file == "-":
            return sys.stdin.read()
        with open(args.spec_file, encoding="utf-8") as f:
            return f.read()
    return sys.stdin.read()


def _resolve_stderr(args) -> str | None:
    if args.sandbox_stderr is not None:
        return args.sandbox_stderr
    if args.sandbox_stderr_file:
        with open(args.sandbox_stderr_file, encoding="utf-8") as f:
            return f.read()
    return None


def cmd_classify_spec(argv) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk address-issue classify-spec",
        description=(
            "Classify a Step 4.1 failing-test spec. Accepts the spec text plus "
            "optional sandbox results and emits a JSON tuple recommending one "
            "of three downstream actions (store / null / discard) and a "
            "test_present score (yes / unverifiable / no)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--spec", default=None, help="Spec text inline.")
    src.add_argument(
        "--spec-file",
        default=None,
        metavar="PATH",
        help="Read spec from PATH ('-' for stdin).",
    )

    parser.add_argument(
        "--sandbox-exit",
        type=int,
        default=None,
        help="Exit code from the Step 4.1.c sandbox run.",
    )
    sb = parser.add_mutually_exclusive_group()
    sb.add_argument(
        "--sandbox-stderr",
        default=None,
        help="Sandbox stderr text (inline).",
    )
    sb.add_argument(
        "--sandbox-stderr-file",
        default=None,
        metavar="PATH",
        help="Read sandbox stderr from PATH.",
    )

    parser.add_argument(
        "--exit-zero-decision",
        choices=["keep", "discard"],
        default=None,
        help=(
            "Implementer's choice when sandbox-exit is 0: 'keep' scores "
            "test_present=unverifiable, 'discard' (default) scores "
            "test_present=no."
        ),
    )

    args = parser.parse_args(argv)

    try:
        spec = _resolve_spec(args)
    except OSError as e:
        print(f"Error reading spec: {e}", file=sys.stderr)
        return 2
    try:
        sandbox_stderr = _resolve_stderr(args)
    except OSError as e:
        print(f"Error reading sandbox stderr: {e}", file=sys.stderr)
        return 2

    # Strip a trailing newline (heredocs and printf '%s\n' append one) but
    # preserve internal newlines so multi-line bash blocks classify correctly.
    if spec.endswith("\n"):
        spec = spec[:-1]
    if not spec:
        print("Error: spec is empty", file=sys.stderr)
        return 2

    effective_first_token = resolve_effective_first_token(spec)
    on_path = is_on_sandbox_path(effective_first_token)

    if args.sandbox_exit is None:
        # Pre-flight only. The fast-path (issue #589 short-circuit + on-PATH
        # check) decides whether the sandbox needs to run at all.
        if not on_path:
            result = {
                "action": "null",
                "test_present": "unverifiable",
                "reason": (
                    f"fast-path skip: effective first token "
                    f"'{effective_first_token}' off sandbox PATH (sandbox "
                    f"would exit 127)"
                ),
                "effective_first_token": effective_first_token,
                "on_path": on_path,
            }
            sys.stdout.write(json.dumps(result) + "\n")
            return 0
        # On-PATH: caller must run the sandbox and re-invoke. Signalled via
        # exit 2 + stderr message rather than a fourth action value, so the
        # classify-spec output schema stays exactly {store, null, discard}.
        print(
            f"Sandbox required: effective first token "
            f"'{effective_first_token}' resolves on sandbox PATH "
            f"({SANDBOX_PATH}). Run the Step 4.1.c sandbox snippet and "
            f"re-invoke with --sandbox-exit and --sandbox-stderr-file.",
            file=sys.stderr,
        )
        return 2

    classification = classify_post_sandbox(
        args.sandbox_exit, sandbox_stderr or "", args.exit_zero_decision,
        effective_first_token,
    )
    classification["effective_first_token"] = effective_first_token
    classification["on_path"] = on_path
    sys.stdout.write(json.dumps(classification) + "\n")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__.lstrip())
        return 0
    sub = argv[0]
    if sub == "classify-spec":
        return cmd_classify_spec(argv[1:])
    print(
        f"Unknown subcommand: {sub}\n"
        "Usage: tusk address-issue classify-spec [options]\n"
        "Run 'tusk address-issue --help' for details.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
