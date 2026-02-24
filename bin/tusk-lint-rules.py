#!/usr/bin/env python3
"""Manage DB-backed lint rules for tusk lint.

Called by the tusk wrapper:
    tusk lint-rule add <pattern> <file_glob> <message> [--blocking] [--skill <name>]
    tusk lint-rule list
    tusk lint-rule remove <id>

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import sqlite3
import sys


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_add(args: argparse.Namespace, db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO lint_rules (grep_pattern, file_glob, message, is_blocking, source_skill)"
            " VALUES (?, ?, ?, ?, ?)",
            (args.pattern, args.file_glob, args.message,
             1 if args.blocking else 0,
             args.skill),
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
            "SELECT id, grep_pattern, file_glob, message, is_blocking, source_skill, created_at"
            " FROM lint_rules ORDER BY id"
        ).fetchall()
        if not rows:
            print("No lint rules defined.")
            return 0
        fmt = "{:<5} {:<10} {:<20} {:<35} {}"
        print(fmt.format("ID", "BLOCKING", "FILE_GLOB", "PATTERN", "MESSAGE"))
        print("-" * 80)
        for row in rows:
            blocking = "yes" if row["is_blocking"] else "no"
            pattern = row["grep_pattern"]
            if len(pattern) > 33:
                pattern = pattern[:30] + "..."
            message = row["message"]
            print(fmt.format(row["id"], blocking, row["file_glob"], pattern, message))
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


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: tusk lint-rule {add|list|remove} ...", file=sys.stderr)
        return 2

    db_path = argv[1]
    # argv[2] is config_path (unused but present for consistency)
    subcommand = argv[3] if len(argv) > 3 else ""

    if subcommand == "add":
        parser = argparse.ArgumentParser(prog="tusk lint-rule add")
        parser.add_argument("pattern", help="grep pattern to search for")
        parser.add_argument("file_glob", help="file glob to search (e.g. '**/*.py')")
        parser.add_argument("message", help="violation message to display")
        parser.add_argument("--blocking", action="store_true",
                            help="make this rule blocking (counts toward lint exit code)")
        parser.add_argument("--skill", default=None, metavar="NAME",
                            help="skill that created this rule")
        args = parser.parse_args(argv[4:])
        return cmd_add(args, db_path)

    elif subcommand == "list":
        return cmd_list(db_path)

    elif subcommand == "remove":
        parser = argparse.ArgumentParser(prog="tusk lint-rule remove")
        parser.add_argument("id", type=int, help="rule ID to remove")
        args = parser.parse_args(argv[4:])
        return cmd_remove(args, db_path)

    else:
        print(f"Unknown subcommand: {subcommand!r}", file=sys.stderr)
        print("Usage: tusk lint-rule {add|list|remove} ...", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
