#!/usr/bin/env python3
"""Manage project conventions.

Called by the tusk wrapper:
    tusk conventions add|list|search|remove ...

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
import tusk_loader  # loads tusk-db-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        topics = args.topics
        if topics:
            topics = ",".join(t.strip() for t in topics.split(","))
        conn.execute(
            "INSERT INTO conventions (text, source_skill, topics) VALUES (?, ?, ?)",
            (args.text, args.skill, topics),
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"Added convention #{new_id}")
        return 0
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        if args.topic:
            rows = conn.execute(
                "SELECT id, text, source_skill, violation_count, topics "
                "FROM conventions "
                "WHERE ',' || topics || ',' LIKE ? "
                "ORDER BY id",
                (f"%,{args.topic},%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, text, source_skill, violation_count, topics "
                "FROM conventions ORDER BY id"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No conventions defined. Use: tusk conventions add \"<text>\"")
        return 0

    print(f"{'ID':<6} {'Skill':<18} {'Violations':<12} {'Topics':<20} {'Text'}")
    print("-" * 100)
    for r in rows:
        skill_str = r["source_skill"] or ""
        topics_str = r["topics"] or ""
        print(f"{r['id']:<6} {skill_str:<18} {r['violation_count']:<12} {topics_str:<20} {r['text']}")
    print(f"\nTotal: {len(rows)}")
    return 0


def cmd_search(args: argparse.Namespace, db_path: str, config: dict) -> int:
    term = f"%{args.term}%"
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, text, source_skill, violation_count, topics "
            "FROM conventions "
            "WHERE text LIKE ? OR topics LIKE ? "
            "ORDER BY id",
            (term, term),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"No conventions matching '{args.term}'.")
        return 0

    print(f"{'ID':<6} {'Skill':<18} {'Violations':<12} {'Topics':<20} {'Text'}")
    print("-" * 100)
    for r in rows:
        skill_str = r["source_skill"] or ""
        topics_str = r["topics"] or ""
        print(f"{r['id']:<6} {skill_str:<18} {r['violation_count']:<12} {topics_str:<20} {r['text']}")
    print(f"\nTotal: {len(rows)}")
    return 0


def cmd_remove(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, text FROM conventions WHERE id = ?", (args.id,)
        ).fetchone()
        if not row:
            print(f"Error: Convention #{args.id} not found", file=sys.stderr)
            return 2

        conn.execute("DELETE FROM conventions WHERE id = ?", (args.id,))
        conn.commit()
        print(f"Removed convention #{args.id}: {row['text']}")
        return 0
    finally:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: tusk conventions {add|list|search|remove} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]
    config = load_config(config_path)

    parser = argparse.ArgumentParser(
        prog="tusk conventions",
        description="Manage project conventions",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # add
    add_p = subparsers.add_parser("add", help="Add a convention")
    add_p.add_argument("text", help="Convention text")
    add_p.add_argument("--skill", default=None, metavar="NAME", help="Source skill name (optional)")
    add_p.add_argument("--topics", default=None, metavar="TOPICS", help="Comma-separated topic tags (e.g. 'zsh,cli,git')")

    # list
    list_p = subparsers.add_parser("list", help="List all conventions")
    list_p.add_argument("--topic", default=None, metavar="TOPIC", help="Filter by topic tag")

    # search
    search_p = subparsers.add_parser("search", help="Search conventions by term (matches text and topics)")
    search_p.add_argument("term", help="Search term (case-insensitive)")

    # remove
    remove_p = subparsers.add_parser("remove", help="Remove a convention by ID")
    remove_p.add_argument("id", type=int, help="Convention ID")

    args = parser.parse_args(sys.argv[3:])

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        handlers = {
            "add": cmd_add,
            "list": cmd_list,
            "search": cmd_search,
            "remove": cmd_remove,
        }
        sys.exit(handlers[args.command](args, db_path, config))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
