#!/usr/bin/env python3
"""Manage project pillars.

Called by the tusk wrapper:
    tusk pillars list|add|remove|set-claim ...

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — subcommand + flags
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, core_claim FROM pillars ORDER BY id"
        ).fetchall()
        result = [{"id": r[0], "name": r[1], "core_claim": r[2]} for r in rows]
        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()


def cmd_add(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO pillars (name, core_claim) VALUES (?, ?)",
            (args.name, args.claim),
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(json.dumps({"id": new_id}))
        return 0
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint" in str(e):
            print(f"Error: pillar '{args.name}' already exists", file=sys.stderr)
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_remove(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("DELETE FROM pillars WHERE name = ?", (args.name,))
        conn.commit()
        if cursor.rowcount == 0:
            print(f"Error: pillar '{args.name}' not found", file=sys.stderr)
            return 1
        print(f"Removed pillar '{args.name}'")
        return 0
    finally:
        conn.close()


def cmd_set_claim(args: argparse.Namespace, db_path: str, config: dict) -> int:
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "UPDATE pillars SET core_claim = ? WHERE name = ?",
            (args.claim, args.name),
        )
        conn.commit()
        if cursor.rowcount == 0:
            print(f"Error: pillar '{args.name}' not found", file=sys.stderr)
            return 1
        print(f"Updated claim for pillar '{args.name}'")
        return 0
    finally:
        conn.close()


# ── Argument parsing ─────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tusk pillars",
        description="Manage project pillars",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="List all pillars as JSON")

    p_add = sub.add_parser("add", help="Add a new pillar")
    p_add.add_argument("--name", required=True, help="Pillar name")
    p_add.add_argument("--claim", required=True, help="Core claim (one-sentence description)")

    p_remove = sub.add_parser("remove", help="Remove a pillar by name")
    p_remove.add_argument("name", help="Pillar name to remove")

    p_set = sub.add_parser("set-claim", help="Update the core claim of a pillar")
    p_set.add_argument("name", help="Pillar name")
    p_set.add_argument("claim", help="New core claim")

    return parser


# ── Entry point ───────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: tusk pillars {list|add|remove|set-claim} ...", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    config_path = sys.argv[2]
    rest = sys.argv[3:]

    config = load_config(config_path)

    parser = build_parser()
    args = parser.parse_args(rest)

    handlers = {
        "list": cmd_list,
        "add": cmd_add,
        "remove": cmd_remove,
        "set-claim": cmd_set_claim,
    }
    sys.exit(handlers[args.subcommand](args, db_path, config))


if __name__ == "__main__":
    main()
