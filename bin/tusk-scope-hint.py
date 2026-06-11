#!/usr/bin/env python3
"""Emit scope suggestions for a draft task — heuristics used by /create-task
to populate ``--scope`` / ``--creates`` / ``--unbounded`` before calling
``tusk task-insert``.

Called by the tusk wrapper:
    tusk scope-hint --summary "..." --description "..." \\
                    [--criterion "..."]... \\
                    [--typed-spec "..."]... \\
                    [--task-type feature] [--domain skills]

Arguments received from tusk:
    sys.argv[1] — DB path (unused; dispatcher-arity parity)
    sys.argv[2] — config path (unused; dispatcher-arity parity)
    sys.argv[3:] — flags above

Output (stdout, JSON):
    {
      "scope": [...],         // paths from the shared auto-scope heuristics
      "creates": [...],       // paths the description marks as newly-created
      "unbounded": true|false,
      "rationale": {
        "scope":     "...",   // populated only when the corresponding
        "creates":   "...",   // suggestion fires
        "unbounded": "..."
      }
    }

The hint is read-only and does not touch the DB or config. It is a
pre-insert helper: ``/create-task`` invokes it during analysis, presents
the proposed scope to the operator for review, and forwards the
confirmed flags to ``tusk task-insert``.

Exit codes:
    0 — success (always, even when every suggestion is empty)
    1 — argument error
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-task-insert.py, tusk-json-lib.py

_task_insert = tusk_loader.load("tusk-task-insert")
_json_lib = tusk_loader.load("tusk-json-lib")
auto_scope_candidates = _task_insert._auto_scope_candidates
resolve_auto_derived_scope_pattern = _task_insert._resolve_auto_derived_scope_pattern
repo_root_for_config = _task_insert._repo_root
dumps = _json_lib.dumps


# A path token for the "creates" regex: dir-prefixed path with a 2-9 char
# extension. Matches what extract_paths considers a real path, minus the
# bare-toplevel whitelist (creates suggestions are always pointing at
# fresh files, not VERSION/README.md/etc.).
_CREATES_PATH = r"[\w./-]+\.[A-Za-z][\w]{1,9}"

# "create / add / introduce / write / generate a new {file|script|...} <path>"
_CREATE_VERB = (
    r"(?:creat(?:e|es|ed|ing)|"
    r"add(?:s|ed|ing)?|"
    r"introduc(?:e|es|ed|ing)|"
    r"writ(?:e|es|ten|ing)|"
    r"generat(?:e|es|ed|ing))"
)
_NEW_ARTIFACT = (
    r"(?:a |an )?(?:brand[- ])?new\s+"
    r"(?:file|script|module|helper|skill|command|migration|test|tool)"
)
_CREATE_VERB_NEW_RE = re.compile(
    rf"\b{_CREATE_VERB}\s+{_NEW_ARTIFACT}\s+"
    rf"(?:at\s+|in\s+|named\s+|called\s+|`)?(?P<path>{_CREATES_PATH})",
    re.IGNORECASE,
)
# "new {file|script|...} <path>" without an explicit verb.
_NEW_ONLY_RE = re.compile(
    r"\bnew\s+(?:file|script|module|helper|skill|command|migration|test|tool)\s+"
    rf"(?:at\s+|in\s+|named\s+|called\s+|`)?(?P<path>{_CREATES_PATH})",
    re.IGNORECASE,
)

# Signal phrases that indicate cross-cutting / repo-wide work.
_UNBOUNDED_PHRASE_RE = re.compile(
    r"\b("
    r"across\s+(?:all|every)|"
    r"all\s+(?:files|skills|migrations|scripts|tests|modules|hooks|commands|consumers)|"
    r"every\s+(?:file|skill|migration|script|test|module|hook|command|task|consumer)|"
    r"repo[- ]wide|"
    r"each\s+(?:skill|script|migration|test|module|hook|command)|"
    r"rename\s+across|"
    r"sweep\s+through|"
    r"throughout\s+the\s+(?:repo|codebase|project)|"
    r"global\s+(?:rename|refactor)"
    r")\b",
    re.IGNORECASE,
)

# Task types whose default mode is cross-cutting.
_UNBOUNDED_TASK_TYPES = {"refactor"}


def _collect_text_blocks(args: argparse.Namespace) -> list[str]:
    blocks = []
    if args.summary:
        blocks.append(args.summary)
    if args.description:
        blocks.append(args.description)
    for c in args.criterion:
        blocks.append(c)
    for spec in args.typed_spec:
        blocks.append(spec)
    return blocks


def _extract_scope(
    blocks: list[str],
    *,
    repo_root: str | None = None,
    task_type: str | None = None,
) -> list[str]:
    seen: set = set()
    out: list = []
    for text in blocks:
        for p in auto_scope_candidates(text, repo_root=repo_root, task_type=task_type):
            p = resolve_auto_derived_scope_pattern(repo_root, p)
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _extract_creates(blocks: list[str]) -> list[str]:
    """Paths the prose explicitly marks as newly-created."""
    seen: set = set()
    out: list = []
    for text in blocks:
        for m in _CREATE_VERB_NEW_RE.finditer(text):
            p = m.group("path")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        for m in _NEW_ONLY_RE.finditer(text):
            p = m.group("path")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _detect_unbounded(args: argparse.Namespace, blocks: list[str]) -> tuple[bool, str]:
    if args.task_type and args.task_type.lower() in _UNBOUNDED_TASK_TYPES:
        return True, f"task_type='{args.task_type}' typically spans many files"
    for text in blocks:
        m = _UNBOUNDED_PHRASE_RE.search(text)
        if m:
            return True, f"signal phrase '{m.group(1).lower()}'"
    return False, ""


def main(argv: list) -> int:
    if len(argv) < 3:
        print(
            "Usage: tusk-scope-hint.py <db_path> <config_path> [flags...]",
            file=sys.stderr,
        )
        return 1

    # argv[1] (db_path) and argv[2] (config_path) are passed by the
    # dispatcher for arity parity with other tusk subcommands, but the
    # hint is read-only over raw text and uses config_path only to resolve
    # the repo root for suffix-based path hints.

    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk scope-hint",
        description="Emit scope/creates/unbounded suggestions for a draft task",
    )
    parser.add_argument("--summary", default="")
    parser.add_argument("--description", default="")
    parser.add_argument(
        "--criterion", action="append", default=[], metavar="TEXT",
        help="Acceptance criterion text (repeatable)",
    )
    parser.add_argument(
        "--typed-spec", action="append", default=[], dest="typed_spec", metavar="SPEC",
        help="Typed-criterion verification spec (repeatable)",
    )
    parser.add_argument(
        "--task-type", default=None, dest="task_type",
        help="Task type (refactor → unbounded hint)",
    )
    parser.add_argument("--domain", default=None, help="Task domain (informational)")
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv[3:])

    blocks = _collect_text_blocks(args)
    repo_root = repo_root_for_config(argv[2], args.repo_root)
    scope = _extract_scope(blocks, repo_root=repo_root, task_type=args.task_type)
    creates = _extract_creates(blocks)
    unbounded_flag, unbounded_why = _detect_unbounded(args, blocks)

    rationale: dict = {}
    if scope:
        rationale["scope"] = "extracted from summary/description/criteria/specs"
    if creates:
        rationale["creates"] = "description names a path as a new file/script"
    if unbounded_flag:
        rationale["unbounded"] = unbounded_why

    out = {
        "scope": scope,
        "creates": creates,
        "unbounded": unbounded_flag,
        "rationale": rationale,
    }
    print(dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
