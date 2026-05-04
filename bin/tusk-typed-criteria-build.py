#!/usr/bin/env python3
"""Build a properly-escaped --typed-criteria JSON string from an arbitrary spec.

The spec is read from stdin by default, or from --spec-file <path>. The output
is a single-line JSON object on stdout suitable for direct use as a value of
--typed-criteria, e.g.:

    JSON=$(tusk typed-criteria-build --spec-file /tmp/spec)
    tusk task-insert ... --typed-criteria "$JSON"

Or in one shot via command substitution:

    tusk task-insert ... --typed-criteria "$(printf '%s' "$SPEC" | tusk typed-criteria-build)"

This helper exists because shell-quoting a spec that contains a mix of single
quotes, double quotes, and backslashes into the documented --typed-criteria
forms (single-quoted JSON, or env-var double-quoted JSON) is brittle and
routinely produces malformed JSON. Centralising the escape logic in one CLI
keeps callers from reinventing it.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-json-lib.py
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps


def build(spec: str, text: str, ctype: str) -> str:
    """Return a compact JSON object encoding the typed-criteria payload."""
    obj = {"text": text, "type": ctype, "spec": spec}
    return dumps(obj, pretty=False)


def main(argv):
    parser = argparse.ArgumentParser(
        prog="tusk typed-criteria-build",
        description=(
            "Emit a properly-escaped --typed-criteria JSON string for an arbitrary spec. "
            "Reads the spec from stdin by default, or from --spec-file <path>."
        ),
    )
    parser.add_argument(
        "--spec-file",
        default=None,
        metavar="PATH",
        help="Read spec from PATH instead of stdin",
    )
    parser.add_argument(
        "--text",
        default="Failing test passes",
        help="Criterion text (default: 'Failing test passes')",
    )
    parser.add_argument(
        "--type",
        default="test",
        dest="ctype",
        help="Criterion type (default: 'test')",
    )
    args = parser.parse_args(argv)

    if args.spec_file:
        try:
            with open(args.spec_file, encoding="utf-8") as f:
                spec = f.read()
        except OSError as e:
            print(f"Error reading --spec-file {args.spec_file}: {e}", file=sys.stderr)
            return 2
    else:
        spec = sys.stdin.read()

    # Strip exactly one trailing newline. Heredocs and `printf '%s\n'` always
    # append one; preserving it bloats the stored spec without changing
    # behaviour. Internal newlines are preserved verbatim.
    if spec.endswith("\n"):
        spec = spec[:-1]

    if not spec:
        print("Error: spec is empty", file=sys.stderr)
        return 2

    print(build(spec, args.text, args.ctype))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
