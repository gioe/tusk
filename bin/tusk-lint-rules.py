#!/usr/bin/env python3
"""Manage DB-backed lint rules for tusk lint.

Called by the tusk wrapper:
    tusk lint-rule add <pattern> <file_glob> <message> [--blocking] [--advisory]
                       [--skill <name>]
    tusk lint-rule propose <pattern> <file_glob> <message>
                           [--finding-id <id>] [--skill <name>]
    tusk lint-rule list
    tusk lint-rule update <id> [--file-glob <glob>] [--grep-pattern <pattern>]
                               [--message <text>] [--blocking | --no-blocking]
                               [--skill <name>]
    tusk lint-rule promote <id>
    tusk lint-rule remove <id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection


def cmd_add(args: argparse.Namespace, db_path: str) -> int:
    enforcement = "advisory" if args.advisory else "enforcing"
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO lint_rules"
            " (grep_pattern, file_glob, message, is_blocking, source_skill, enforcement)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (args.pattern, args.file_glob, args.message,
             1 if args.blocking else 0,
             args.skill,
             enforcement),
        )
        conn.commit()
        print(cur.lastrowid)
        return 0
    finally:
        conn.close()


def cmd_propose(args: argparse.Namespace, db_path: str) -> int:
    """Stage a grep-detectable anti-pattern surfaced by /retro as an advisory rule.

    Always inserts ``enforcement='advisory'`` and ``is_blocking=1`` so the rule
    warns on hits but never gates ``tusk lint``/``commit``/``merge`` until
    ``tusk lint-rule promote`` flips it to enforcing once the pattern is observed
    to hold. The originating retro finding is recorded in ``source_finding_id``
    for provenance back to the retrospective that proposed the rule. When
    ``--finding-id`` is supplied it is validated as a real retro_findings FK
    before the insert so a typo'd id fails fast.
    """
    conn = get_connection(db_path)
    try:
        if args.finding_id is not None:
            finding = conn.execute(
                "SELECT id FROM retro_findings WHERE id = ?", (args.finding_id,)
            ).fetchone()
            if not finding:
                print(
                    f"Error: retro finding {args.finding_id} not found",
                    file=sys.stderr,
                )
                return 2
        cur = conn.execute(
            "INSERT INTO lint_rules"
            " (grep_pattern, file_glob, message, is_blocking, source_skill,"
            "  enforcement, source_finding_id)"
            " VALUES (?, ?, ?, 1, ?, 'advisory', ?)",
            (args.pattern, args.file_glob, args.message,
             args.skill, args.finding_id),
        )
        conn.commit()
        print(cur.lastrowid)
        return 0
    finally:
        conn.close()


def cmd_list(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, grep_pattern, file_glob, message, is_blocking, source_skill,"
            " enforcement, created_at"
            " FROM lint_rules ORDER BY id"
        ).fetchall()
        if not rows:
            print("No lint rules defined.")
            return 0
        fmt = "{:<5} {:<10} {:<10} {:<18} {:<30} {}"
        print(fmt.format("ID", "BLOCKING", "ENFORCE", "FILE_GLOB", "PATTERN", "MESSAGE"))
        print("-" * 90)
        for row in rows:
            blocking = "yes" if row["is_blocking"] else "no"
            enforcement = row["enforcement"]
            pattern = row["grep_pattern"]
            if len(pattern) > 28:
                pattern = pattern[:25] + "..."
            message = row["message"]
            print(fmt.format(
                row["id"], blocking, enforcement, row["file_glob"], pattern, message))
        return 0
    finally:
        conn.close()


def cmd_update(args: argparse.Namespace, db_path: str) -> int:
    updates: list[str] = []
    params: list = []
    if args.file_glob is not None:
        updates.append("file_glob = ?")
        params.append(args.file_glob)
    if args.grep_pattern is not None:
        updates.append("grep_pattern = ?")
        params.append(args.grep_pattern)
    if args.message is not None:
        updates.append("message = ?")
        params.append(args.message)
    if args.is_blocking is not None:
        updates.append("is_blocking = ?")
        params.append(args.is_blocking)
    if args.skill is not None:
        updates.append("source_skill = ?")
        params.append(args.skill)

    if not updates:
        print(
            "Error: no fields to update; pass at least one of"
            " --file-glob, --grep-pattern, --message, --blocking/--no-blocking, --skill",
            file=sys.stderr,
        )
        return 2

    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM lint_rules WHERE id = ?", (args.id,)
        ).fetchone()
        if not existing:
            print(f"Error: lint rule {args.id} not found", file=sys.stderr)
            return 2
        params.append(args.id)
        conn.execute(
            f"UPDATE lint_rules SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        print(args.id)
        return 0
    finally:
        conn.close()


def cmd_remove(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM lint_rules WHERE id = ?", (args.id,)
        ).fetchone()
        if not existing:
            print(f"Error: lint rule {args.id} not found", file=sys.stderr)
            return 2
        conn.execute("DELETE FROM lint_rules WHERE id = ?", (args.id,))
        conn.commit()
        print(f"Removed lint rule {args.id}.")
        return 0
    finally:
        conn.close()


def cmd_promote(args: argparse.Namespace, db_path: str) -> int:
    """Flip an advisory rule to enforcing once it has been observed to hold."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, enforcement FROM lint_rules WHERE id = ?", (args.id,)
        ).fetchone()
        if not row:
            print(f"Error: lint rule {args.id} not found", file=sys.stderr)
            return 2
        if row["enforcement"] == "enforcing":
            print(f"Lint rule {args.id} is already enforcing.")
            return 0
        conn.execute(
            "UPDATE lint_rules SET enforcement = 'enforcing' WHERE id = ?",
            (args.id,),
        )
        conn.commit()
        print(f"Promoted lint rule {args.id} to enforcing.")
        return 0
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk lint-rule {add|propose|list|update|promote|remove} ...", file=sys.stderr)
        return 2

    db_path = argv[1]
    # argv[2] is config_path (unused but present for consistency)
    subcommand = argv[3] if len(argv) > 3 else ""

    if subcommand == "add":
        parser = argparse.ArgumentParser(allow_abbrev=False, prog="tusk lint-rule add")
        parser.add_argument("pattern", help="grep pattern to search for")
        parser.add_argument("file_glob",
                            help="file glob to search (e.g. '**/*.py'). Pass a comma-separated"
                                 " list to scope multiple paths"
                                 " (e.g. 'skills/**/*.md,codex-prompts/**/*.md').")
        parser.add_argument("message", help="violation message to display")
        parser.add_argument("--blocking", action="store_true",
                            help="make this rule blocking (counts toward lint exit code)")
        parser.add_argument("--advisory", action="store_true",
                            help="stage this rule advisory-only — hits warn but never gate "
                                 "tusk lint/commit/merge until `tusk lint-rule promote` "
                                 "flips it to enforcing")
        parser.add_argument("--skill", default=None, metavar="NAME",
                            help="skill that created this rule")
        args = parser.parse_args(argv[4:])
        return cmd_add(args, db_path)

    elif subcommand == "propose":
        parser = argparse.ArgumentParser(allow_abbrev=False, prog="tusk lint-rule propose")
        parser.add_argument("pattern", help="grep pattern to search for")
        parser.add_argument("file_glob",
                            help="file glob to search (e.g. '**/*.py'). Pass a comma-separated"
                                 " list to scope multiple paths"
                                 " (e.g. 'skills/**/*.md,codex-prompts/**/*.md').")
        parser.add_argument("message", help="violation message to display")
        parser.add_argument("--finding-id", dest="finding_id", type=int, default=None,
                            metavar="ID",
                            help="retro_findings id this rule was proposed from "
                                 "(recorded as provenance)")
        parser.add_argument("--skill", default="retro", metavar="NAME",
                            help="skill that proposed this rule (default: retro)")
        args = parser.parse_args(argv[4:])
        return cmd_propose(args, db_path)

    elif subcommand == "list":
        return cmd_list(db_path)

    elif subcommand == "update":
        parser = argparse.ArgumentParser(allow_abbrev=False, prog="tusk lint-rule update")
        parser.add_argument("id", type=int, help="rule ID to update")
        parser.add_argument("--file-glob", dest="file_glob", default=None,
                            help="new file glob (comma-separated for multiple paths)")
        parser.add_argument("--grep-pattern", dest="grep_pattern", default=None,
                            help="new grep pattern")
        parser.add_argument("--message", default=None,
                            help="new violation message")
        blocking_group = parser.add_mutually_exclusive_group()
        blocking_group.add_argument("--blocking", dest="is_blocking",
                                    action="store_const", const=1, default=None,
                                    help="mark this rule as blocking")
        blocking_group.add_argument("--no-blocking", dest="is_blocking",
                                    action="store_const", const=0,
                                    help="mark this rule as advisory (non-blocking)")
        parser.add_argument("--skill", default=None, metavar="NAME",
                            help="set the skill that owns this rule")
        args = parser.parse_args(argv[4:])
        return cmd_update(args, db_path)

    elif subcommand == "promote":
        parser = argparse.ArgumentParser(allow_abbrev=False, prog="tusk lint-rule promote")
        parser.add_argument("id", type=int,
                            help="rule ID to promote from advisory to enforcing")
        args = parser.parse_args(argv[4:])
        return cmd_promote(args, db_path)

    elif subcommand == "remove":
        parser = argparse.ArgumentParser(allow_abbrev=False, prog="tusk lint-rule remove")
        parser.add_argument("id", type=int, help="rule ID to remove")
        args = parser.parse_args(argv[4:])
        return cmd_remove(args, db_path)

    else:
        print(f"Unknown subcommand: {subcommand!r}", file=sys.stderr)
        print("Usage: tusk lint-rule {add|propose|list|update|promote|remove} ...", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk lint-rule {add|propose|list|update|remove} ...", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv))
