#!/usr/bin/env python3
"""Apply durable project memory from a confirmed tusk-init bootstrap plan."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py and tusk-json-lib.py

_db_lib = tusk_loader.load("tusk-db-lib")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection


VALID_CONTEXT_TYPES = {"memory", "assumption", "question", "risk", "decision", "entry_point"}


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]
    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = str(raw).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            items.append(text)
    return items


def _clean_context_atom(atom: dict[str, Any]) -> dict[str, str] | None:
    item_type = atom.get("type")
    content = str(atom.get("content") or "").strip()
    if item_type not in VALID_CONTEXT_TYPES or not content:
        return None
    return {"type": item_type, "content": content}


def _add_unique_context(out: list[dict[str, str]], item_type: str, content: str) -> None:
    atom = _clean_context_atom({"type": item_type, "content": content})
    if atom and atom not in out:
        out.append(atom)


def _title(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", " ").split())


def derive_memory(plan: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Derive context atoms, pillars, and glossary entries from a bootstrap plan."""
    context_atoms: list[dict[str, str]] = []
    pillars: list[dict[str, str]] = []
    glossary: list[dict[str, str]] = []

    for atom in plan.get("context_atoms") or []:
        cleaned = _clean_context_atom(atom)
        if cleaned and cleaned not in context_atoms:
            context_atoms.append(cleaned)

    for pillar in plan.get("pillars") or []:
        name = str(pillar.get("name") or "").strip()
        claim = str(pillar.get("claim") or pillar.get("core_claim") or "").strip()
        entry = {"name": name, "claim": claim}
        if name and claim and entry not in pillars:
            pillars.append(entry)

    for entry in plan.get("glossary") or []:
        term = str(entry.get("term") or "").strip()
        definition = str(entry.get("definition") or "").strip()
        cleaned = {"term": term, "definition": definition}
        if term and definition and cleaned not in glossary:
            glossary.append(cleaned)

    intent = ((plan.get("intent") or {}).get("init_intent") or {})
    archetype = plan.get("archetype") or {}
    label = str(archetype.get("label") or "").strip()
    rationale = str(archetype.get("rationale") or "").strip()
    if label and rationale:
        _add_unique_context(context_atoms, "decision", f"Inferred archetype: {label} — {rationale}")
    elif label:
        _add_unique_context(context_atoms, "decision", f"Inferred archetype: {label}")

    audience = str(intent.get("audience") or "").strip()
    if audience:
        _add_unique_context(context_atoms, "memory", f"Audience: {audience}")
    for workflow in _list(intent.get("primary_workflows")):
        _add_unique_context(context_atoms, "memory", f"Primary workflow: {workflow}")
    for integration in _list(intent.get("integrations")):
        _add_unique_context(context_atoms, "memory", f"Integration: {integration}")
    for data_need in _list(intent.get("data_needs")):
        _add_unique_context(context_atoms, "memory", f"Data need: {data_need}")
    for priority in _list(intent.get("quality_priorities")):
        _add_unique_context(context_atoms, "decision", f"Quality priority: {priority}")
        name = _title(priority)
        pillar = {
            "name": name,
            "claim": f"The system should make {priority} a first-class quality priority.",
        }
        if pillar not in pillars:
            pillars.append(pillar)
    for non_goal in _list(intent.get("non_goals")):
        _add_unique_context(context_atoms, "assumption", f"Non-goal: {non_goal}")
    for question in _list(intent.get("open_questions")):
        _add_unique_context(context_atoms, "question", f"Open question: {question}")

    return {"context_atoms": context_atoms, "pillars": pillars, "glossary": glossary}


def plan_memory_application(memory: dict[str, list[dict[str, str]]], existing: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    existing_context = {
        (row.get("type") or row.get("item_type"), row.get("content"))
        for row in existing.get("context_atoms", [])
    }
    existing_pillars = {
        row.get("name")
        for row in existing.get("pillars", [])
    }
    existing_glossary = {
        row.get("term")
        for row in existing.get("glossary", [])
    }

    context_to_add = []
    skipped_context = []
    for atom in memory.get("context_atoms", []):
        if (atom["type"], atom["content"]) in existing_context:
            skipped_context.append(atom)
        else:
            context_to_add.append(atom)

    pillars_to_add = []
    skipped_pillars = []
    for pillar in memory.get("pillars", []):
        if pillar["name"] in existing_pillars:
            skipped_pillars.append(pillar)
        else:
            pillars_to_add.append(pillar)

    glossary_to_add = []
    skipped_glossary = []
    for entry in memory.get("glossary", []):
        if entry["term"] in existing_glossary:
            skipped_glossary.append(entry)
        else:
            glossary_to_add.append(entry)

    return {
        "context_atoms_to_add": context_to_add,
        "skipped_context_atoms": skipped_context,
        "pillars_to_add": pillars_to_add,
        "skipped_pillars": skipped_pillars,
        "glossary_to_add": glossary_to_add,
        "skipped_glossary": skipped_glossary,
    }


def _ensure_task_exists(conn: sqlite3.Connection, task_id: int) -> None:
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} not found")


def _existing_memory(conn: sqlite3.Connection, task_id: int) -> dict[str, list[dict[str, str]]]:
    context_rows = conn.execute(
        "SELECT item_type, content FROM task_context_items WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    pillar_rows = conn.execute("SELECT name, core_claim FROM pillars").fetchall()
    glossary_rows = conn.execute("SELECT term, definition FROM glossary").fetchall()
    return {
        "context_atoms": [{"type": row[0], "content": row[1]} for row in context_rows],
        "pillars": [{"name": row[0], "core_claim": row[1]} for row in pillar_rows],
        "glossary": [{"term": row[0], "definition": row[1]} for row in glossary_rows],
    }


def apply_memory(db_path: str, plan: dict[str, Any], task_id: int) -> dict[str, Any]:
    memory = derive_memory(plan)
    conn = get_connection(db_path)
    try:
        _ensure_task_exists(conn, task_id)
        application = plan_memory_application(memory, _existing_memory(conn, task_id))
        added_context_ids: list[int] = []
        for atom in application["context_atoms_to_add"]:
            cursor = conn.execute(
                "INSERT INTO task_context_items "
                "  (task_id, item_type, content, source) "
                "VALUES (?, ?, ?, 'agent_handoff')",
                (task_id, atom["type"], atom["content"]),
            )
            added_context_ids.append(int(cursor.lastrowid))
        for pillar in application["pillars_to_add"]:
            conn.execute(
                "INSERT INTO pillars (name, core_claim) VALUES (?, ?)",
                (pillar["name"], pillar["claim"]),
            )
        for entry in application["glossary_to_add"]:
            conn.execute(
                "INSERT INTO glossary (term, definition) VALUES (?, ?)",
                (entry["term"], entry["definition"]),
            )
        conn.commit()
        return {
            "task_id": task_id,
            "added_context_atoms": added_context_ids,
            "skipped_context_atoms": application["skipped_context_atoms"],
            "added_pillars": [p["name"] for p in application["pillars_to_add"]],
            "skipped_pillars": [p["name"] for p in application["skipped_pillars"]],
            "added_glossary": [g["term"] for g in application["glossary_to_add"]],
            "skipped_glossary": [g["term"] for g in application["skipped_glossary"]],
        }
    finally:
        conn.close()


def _parse_plan(raw: str) -> dict[str, Any]:
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise ValueError("--plan must be a JSON object")
    return plan


def main(argv: list[str]) -> int:
    db_path = argv[0]
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk init-apply-memory",
        description="Apply durable project memory from a confirmed bootstrap plan.",
    )
    parser.add_argument("--plan", required=True, help="Bootstrap plan JSON object.")
    parser.add_argument("--task-id", required=True, type=int, help="Task that receives context atoms.")
    args = parser.parse_args(argv[2:])

    try:
        result = apply_memory(db_path, _parse_plan(args.plan), args.task_id)
    except ValueError as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1
    except sqlite3.IntegrityError as exc:
        print(dumps({"success": False, "error": str(exc)}))
        return 1

    print(dumps({"success": True, **result}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk init-apply-memory --plan '<json>' --task-id <id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(_db_lib.retry_on_locked(lambda: main(sys.argv[1:]), label="init-apply-memory"))
