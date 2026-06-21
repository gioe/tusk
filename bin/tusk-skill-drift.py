#!/usr/bin/env python3
"""
tusk-skill-drift — detect skill/CLI subcommand drift.

Distributed skills (`.claude/skills/*/SKILL.md`) prescribe `tusk <subcommand>`
invocations. When a project's installed CLI lags the skills it ships with
(version skew), those skills reference subcommands the CLI doesn't have, and the
operator hits cryptic "Unknown subcommand" errors mid-workflow while scope/audit
guardrails silently can't run (GitHub issue #1035).

This helper compares the `tusk <subcommand>` references found in the installed
skills against the **installed** CLI's own dispatcher. The authoritative
subcommand set is parsed directly from the sibling `bin/tusk` dispatcher
case-arms — the same drift-proof source the Usage message uses (TASK-233) — so
there is no hand-maintained list to fall out of sync. Any referenced subcommand
absent from that set is reported as drift, with a recommendation to run
`tusk upgrade`.

Called by the tusk wrapper:
    tusk skill-drift [--format text|json]
    tusk skill-drift --is-referenced <subcommand>   # exit 0 iff skills reference it

Arguments received from the tusk wrapper:
    sys.argv[1] — REPO_ROOT
    sys.argv[2:] — caller args

Exit codes (default mode):
    0 — no drift (or no skills found to check)
    1 — at least one referenced subcommand is absent from the installed CLI

Exit codes (--is-referenced mode):
    0 — the named subcommand is referenced by at least one installed skill
    1 — not referenced (or no skills found)
"""

import argparse
import json
import os
import re
import sys

# A `tusk <token>` reference where the char before `tusk` is not a word char or
# hyphen (so `bin/tusk scope` matches but `tusk-lint`/`pytusk` do not), and the
# token is a lowercase subcommand-shaped word (so flags like `-json`, SQL like
# `"SELECT`, and placeholders like `<id>` are skipped). The token captured is the
# FIRST word after `tusk`, which is the dispatched subcommand — for `tusk review
# begin` the token is `review`, not `begin`.
_TUSK_REF = re.compile(r"(?<![\w-])tusk\s+([a-z][a-z0-9-]*)")

# A plain English word (no shell-special chars). Used to reject prose like
# "Originating tusk task" / "a tusk client" that appears inside fenced markdown
# templates: there `tusk` is preceded by an ordinary word, whereas a real CLI
# invocation has `tusk` at command position (start, after a shell operator, or as
# a `bin/tusk` path). Env-assignments (`TUSK_DB=…`) and paths contain `=` / `/`
# and so are NOT plain words — those invocations are kept.
_PLAIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*\Z")


def _is_command_position(prefix: str) -> bool:
    """Decide whether `tusk`, occurring after `prefix` within a code chunk, is a
    genuine CLI invocation rather than a `/tusk` slash-command or a prose mention.

    Kept: start-of-chunk, after a shell operator / `(` / backtick / `$`, or a
    `bin/tusk`-style path. Rejected: `/tusk` slash-command (skill invocation) and
    `<word> tusk` prose."""
    if prefix == "":
        return True
    if prefix.endswith("/"):
        # `bin/tusk` (path) vs `/tusk` (slash-command). The slash-command form has
        # the `/` at word-start (preceded by whitespace or nothing).
        before_slash = prefix[:-1]
        return bool(before_slash) and not before_slash[-1].isspace()
    if prefix[-1].isspace():
        token = prefix.split()[-1] if prefix.split() else ""
        # Leading indentation only (no preceding token) → command at line start.
        if token == "":
            return True
        # A plain English word before `tusk` → prose mention, not a command.
        return not _PLAIN_WORD.match(token)
    # Preceded by a shell operator / paren / backtick / `$` etc. → command.
    return True


def _iter_subcommand_tokens(chunk: str):
    """Yield the dispatched subcommand token for every genuine `tusk <subcommand>`
    invocation in `chunk` (a code-context substring)."""
    for m in _TUSK_REF.finditer(chunk):
        if _is_command_position(chunk[: m.start()]):
            yield m.group(1)

# Match a dispatcher case-arm: two-space indent, a subcommand-shaped name, then
# `)`. Mirrors the sed extraction at bin/tusk's empty-arg Usage branch
# (`s/^  \([a-z][a-z0-9-]*\)).*/\1/p`). The `""` and `*` fallback arms do not
# match (they start with `"` / `*`, not `[a-z]`).
_CASE_ARM = re.compile(r"^  ([a-z][a-z0-9-]*)\)")


def installed_tusk_path() -> str:
    """Resolve the sibling `bin/tusk` shipped alongside this helper.

    In the source repo this is `bin/tusk`; in a consumer install it is
    `.claude/bin/tusk`. Either way it is the CLI whose dispatch table we must
    compare the skills against.
    """
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), "tusk")


def parse_installed_subcommands(tusk_path: str) -> set:
    """Extract the authoritative subcommand set from the dispatcher case-arms.

    Reads only the block between `case "${1:-}" in` and the matching `esac` so
    that unrelated `case` statements elsewhere in the script cannot leak names.
    """
    try:
        with open(tusk_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return set()

    # Anchor on the dispatcher's column-0 `case`/`esac`, exactly as the Usage
    # message's sed extraction does (`/^case "\${1:-}" in$/,/^esac$/`). Helper
    # functions (is_readonly_subcmd, preflight_schema_version, …) contain their
    # own *indented* `case "${1:-}" in` blocks — matching on a stripped line
    # would latch onto the first of those and parse zero dispatch arms.
    subcmds = set()
    in_dispatch = False
    for line in lines:
        if not in_dispatch:
            if line == 'case "${1:-}" in':
                in_dispatch = True
            continue
        if line == "esac":
            break
        m = _CASE_ARM.match(line)
        if m:
            subcmds.add(m.group(1))
    return subcmds


def _scan_code_context(text: str):
    """Yield the substrings of `text` that are in Markdown code context.

    Command references in SKILL.md are nearly always code-formatted — fenced
    blocks (``` / ~~~) or inline spans (`...`). Restricting the scan to code
    context is what keeps prose like "the tusk database" or "run tusk and then"
    from being misread as subcommand references (false-positive guard for the
    no-drift-in-source-repo invariant).
    """
    fence = None  # the active fence marker (``` or ~~~), or None
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                continue
            # Outside a fence: only the contents of inline-code spans count.
            for span in re.findall(r"`+([^`]+)`+", raw):
                yield span
        else:
            if stripped.startswith(fence):
                fence = None
                continue
            yield raw


def referenced_subcommands(skill_files):
    """Map each `tusk <subcommand>` token found in code context to the skills
    that reference it. Returns {subcommand: sorted([skill_path, ...])}."""
    refs = {}
    for path in skill_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for chunk in _scan_code_context(text):
            for token in _iter_subcommand_tokens(chunk):
                refs.setdefault(token, set()).add(path)
    return {tok: sorted(paths) for tok, paths in refs.items()}


def find_skill_files(repo_root: str):
    """Collect SKILL.md files across the installed (`.claude/skills`) and
    source-repo (`skills`, `skills-internal`) layouts, deduped by realpath so a
    source repo's symlinked `.claude/skills` is not double-counted."""
    seen = set()
    files = []
    for sub in (".claude/skills", "skills", "skills-internal"):
        base = os.path.join(repo_root, sub)
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            candidate = os.path.join(base, entry, "SKILL.md")
            if not os.path.isfile(candidate):
                continue
            real = os.path.realpath(candidate)
            if real in seen:
                continue
            seen.add(real)
            files.append(candidate)
    return files


def compute_drift(repo_root: str, tusk_path: str = None):
    """Return (drift, refs, installed, skill_files).

    `drift` maps each referenced-but-missing subcommand to the skills that
    reference it. `installed` is the parsed dispatch set."""
    tusk_path = tusk_path or installed_tusk_path()
    installed = parse_installed_subcommands(tusk_path)
    skill_files = find_skill_files(repo_root)
    refs = referenced_subcommands(skill_files)
    drift = {tok: paths for tok, paths in refs.items() if tok not in installed}
    return drift, refs, installed, skill_files


def _rel(repo_root: str, path: str) -> str:
    try:
        return os.path.relpath(path, repo_root)
    except ValueError:
        return path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tusk skill-drift", add_help=True, allow_abbrev=False
    )
    parser.add_argument("repo_root")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--is-referenced",
        metavar="SUBCOMMAND",
        help="exit 0 iff installed skills reference 'tusk <SUBCOMMAND>' (quiet probe)",
    )
    args = parser.parse_args()

    repo_root = args.repo_root

    # Quiet probe used by the unknown-subcommand handler: is this token something
    # the installed skills expect? (Independent of whether the CLI has it.)
    if args.is_referenced is not None:
        skill_files = find_skill_files(repo_root)
        refs = referenced_subcommands(skill_files)
        return 0 if args.is_referenced in refs else 1

    drift, refs, installed, skill_files = compute_drift(repo_root)

    if args.format == "json":
        payload = {
            "drift": [
                {"subcommand": tok, "skills": [_rel(repo_root, p) for p in paths]}
                for tok, paths in sorted(drift.items())
            ],
            "skill_files_scanned": len(skill_files),
            "installed_subcommand_count": len(installed),
            "recommendation": "tusk upgrade" if drift else None,
        }
        print(json.dumps(payload))
        return 1 if drift else 0

    # text format
    if not skill_files:
        print("  No installed skills found to check for drift.")
        return 0
    if not drift:
        print(
            f"  Skill/CLI subcommand drift OK "
            f"({len(skill_files)} skill(s) scanned, "
            f"{len(installed)} installed subcommand(s))."
        )
        return 0

    print(
        f"Skill/CLI subcommand drift: {len(drift)} subcommand(s) referenced by "
        f"installed skills are absent from this CLI:",
        file=sys.stderr,
    )
    for tok, paths in sorted(drift.items()):
        rels = ", ".join(_rel(repo_root, p) for p in paths)
        print(f"  - missing subcommand: tusk {tok}  (referenced in {rels})", file=sys.stderr)
    print(
        "Your installed tusk CLI is behind the skills it ships with. "
        "Run 'tusk upgrade' to install the subcommands the skills expect.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
