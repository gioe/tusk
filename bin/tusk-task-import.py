#!/usr/bin/env python3
"""Import tasks from structured JSON.

Called by the tusk wrapper:
    tusk task-import --file tasks.json [--dry-run]
    tusk task-import --stdin [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads hyphenated tusk modules

_db_lib = tusk_loader.load("tusk-db-lib")
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config
validate_enum = _db_lib.validate_enum

_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps

_task_insert = tusk_loader.load("tusk-task-insert")
TUSK_BIN = _task_insert.TUSK_BIN
run_dupe_check = _task_insert.run_dupe_check
reject_shell_metacharacters = _task_insert.reject_shell_metacharacters
_canonical_enum_value = _task_insert._canonical_enum_value
_expand_scope_patterns = _task_insert._expand_scope_patterns
_parse_not_before = _task_insert._parse_not_before
_repo_root = _task_insert._repo_root
_warn_for_missing_declared_paths = _task_insert._warn_for_missing_declared_paths
_warn_already_passing_criteria = _task_insert._warn_already_passing_criteria
insert_task_record = _task_insert.insert_task_record


@dataclass
class ImportErrorItem:
    field: str
    message: str


@dataclass
class DependencyRef:
    raw_index: int
    key: str | None = None
    task_id: int | None = None
    relationship_type: str = "blocks"


@dataclass
class TaskPlan:
    index: int
    key: str | None
    summary: str
    description: str
    priority: str
    domain: str | None
    task_type: str
    assignee: str | None
    complexity: str
    workflow: str | None
    expires_in_days: int | None
    not_before: str | None
    fixes_task_id: int | None
    scope_patterns: list[str]
    creates_paths: list[str]
    unbounded: bool
    criteria: list[str]
    typed_criteria: list[dict[str, Any]]
    duplicate_policy: str
    deps: list[DependencyRef] = field(default_factory=list)
    dupe: dict[str, Any] | None = None


def _result_shell() -> dict[str, dict[str, Any]]:
    return {"created": {}, "skipped": {}, "failed": {}}


def _failure_entry(key: str | None, errors: list[ImportErrorItem]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "errors": [{"field": e.field, "message": e.message} for e in errors],
    }
    if key is not None:
        entry["key"] = key
    return entry


def _load_json(args: argparse.Namespace) -> tuple[Any | None, list[ImportErrorItem]]:
    try:
        if args.stdin:
            raw = sys.stdin.read()
        else:
            with open(args.file, "r", encoding="utf-8") as fh:
                raw = fh.read()
    except OSError as exc:
        return None, [ImportErrorItem("$", f"unable to read input: {exc}")]

    try:
        return json.loads(raw), []
    except json.JSONDecodeError as exc:
        return None, [ImportErrorItem("$", f"malformed JSON: {exc.msg}")]


def _as_nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_criteria(raw: Any) -> tuple[list[str], list[dict[str, Any]], list[ImportErrorItem]]:
    errors: list[ImportErrorItem] = []
    criteria: list[str] = []
    typed: list[dict[str, Any]] = []
    if not isinstance(raw, list) or not raw:
        return [], [], [ImportErrorItem("criteria", "at least one criterion is required")]

    for i, item in enumerate(raw):
        field = f"criteria[{i}]"
        if isinstance(item, str):
            text = item.strip()
            if not text:
                errors.append(ImportErrorItem(field, "criterion text is required"))
            else:
                criteria.append(item)
            continue
        if not isinstance(item, dict):
            errors.append(ImportErrorItem(field, "criterion must be a string or object"))
            continue
        text = _as_nonempty_string(item.get("text"))
        if text is None:
            errors.append(ImportErrorItem(f"{field}.text", "criterion text is required"))
            continue
        criterion = {"text": text, "type": item.get("type", "manual"), "spec": item.get("spec")}
        typed.append(criterion)
    return criteria, typed, errors


def _normalize_dependencies(raw: Any) -> tuple[list[DependencyRef], list[ImportErrorItem]]:
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [ImportErrorItem("depends_on", "depends_on must be an array")]

    deps: list[DependencyRef] = []
    errors: list[ImportErrorItem] = []
    for i, item in enumerate(raw):
        field = f"depends_on[{i}]"
        rel_type = "blocks"
        key: str | None = None
        task_id: int | None = None
        if isinstance(item, str):
            key = item
        elif isinstance(item, int) and not isinstance(item, bool):
            task_id = item
        elif isinstance(item, dict):
            rel_type = item.get("type", "blocks")
            if "key" in item:
                key = item.get("key")
            elif "id" in item:
                task_id = item.get("id")
            else:
                errors.append(ImportErrorItem(field, "dependency must include key or id"))
                continue
        else:
            errors.append(ImportErrorItem(field, "dependency must be a key, id, or object"))
            continue
        if rel_type not in ("blocks", "contingent"):
            errors.append(ImportErrorItem(f"{field}.type", "type must be 'blocks' or 'contingent'"))
        if key is not None and not _as_nonempty_string(key):
            errors.append(ImportErrorItem(field, "dependency key is required"))
        if task_id is not None and (not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0):
            errors.append(ImportErrorItem(field, "dependency id must be a positive integer"))
        deps.append(DependencyRef(raw_index=i, key=key, task_id=task_id, relationship_type=rel_type))
    return deps, errors


def _normalize_string_list(raw: Any, field_name: str) -> tuple[list[str], list[ImportErrorItem]]:
    if raw is None:
        return [], []
    if isinstance(raw, str):
        return [raw], []
    if not isinstance(raw, list):
        return [], [ImportErrorItem(field_name, f"{field_name} must be a string or array")]

    values: list[str] = []
    errors: list[ImportErrorItem] = []
    for i, item in enumerate(raw):
        text = _as_nonempty_string(item)
        if text is None:
            errors.append(ImportErrorItem(f"{field_name}[{i}]", f"{field_name} value is required"))
        else:
            values.append(text)
    return values, errors


def _normalize_expires_in(raw: Any) -> tuple[int | None, list[ImportErrorItem]]:
    if raw is None:
        return None, []
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None, [ImportErrorItem("expires_in", "expires_in must be an integer number of days")]
    return raw, []


def _normalize_not_before(raw: Any) -> tuple[str | None, list[ImportErrorItem]]:
    if raw is None:
        return None, []
    text = _as_nonempty_string(raw)
    if text is None:
        return None, [ImportErrorItem("not_before", "not_before must be a non-empty string")]
    try:
        return _parse_not_before(text), []
    except argparse.ArgumentTypeError as exc:
        return None, [ImportErrorItem("not_before", str(exc))]


def _normalize_positive_int(raw: Any, field_name: str) -> tuple[int | None, list[ImportErrorItem]]:
    if raw is None:
        return None, []
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        return None, [ImportErrorItem(field_name, f"{field_name} must be a positive integer")]
    return raw, []


def _validate_item(
    item: Any,
    index: int,
    config: dict[str, Any],
    known_keys: set[str],
) -> tuple[TaskPlan | None, list[ImportErrorItem], str | None]:
    if not isinstance(item, dict):
        return None, [ImportErrorItem("$", "task must be an object")], None

    key = item.get("key")
    errors: list[ImportErrorItem] = []
    if key is not None and not _as_nonempty_string(key):
        errors.append(ImportErrorItem("key", "key must be a non-empty string"))
        key = None

    summary = _as_nonempty_string(item.get("summary"))
    if summary is None:
        errors.append(ImportErrorItem("summary", "summary is required"))
        summary = ""
    description = item.get("description")
    if not isinstance(description, str):
        errors.append(ImportErrorItem("description", "description is required"))
        description = ""

    criteria, typed_criteria, criterion_errors = _normalize_criteria(item.get("criteria"))
    errors.extend(criterion_errors)

    priority = _canonical_enum_value(item.get("priority", "Medium"), config.get("priorities", []))
    task_type = item.get("task_type", item.get("task-type", "feature"))
    complexity = item.get("complexity", "M")
    domain = item.get("domain")
    assignee = item.get("assignee")
    workflow = item.get("workflow")
    expires_in_days, expires_errors = _normalize_expires_in(item.get("expires_in"))
    errors.extend(expires_errors)
    not_before, not_before_errors = _normalize_not_before(item.get("not_before"))
    errors.extend(not_before_errors)
    fixes_task_id, fixes_errors = _normalize_positive_int(item.get("fixes_task_id"), "fixes_task_id")
    errors.extend(fixes_errors)
    raw_scope, scope_errors = _normalize_string_list(item.get("scope"), "scope")
    errors.extend(scope_errors)
    scope_patterns = _expand_scope_patterns(raw_scope)
    creates_paths, creates_errors = _normalize_string_list(item.get("creates"), "creates")
    errors.extend(creates_errors)
    unbounded = item.get("unbounded", False)
    if not isinstance(unbounded, bool):
        errors.append(ImportErrorItem("unbounded", "unbounded must be true or false"))
        unbounded = False

    enum_checks = [
        ("priority", priority, config.get("priorities", [])),
        ("task_type", task_type, config.get("task_types", [])),
        ("complexity", complexity, config.get("complexity", [])),
    ]
    if domain is not None:
        enum_checks.append(("domain", domain, config.get("domains", [])))
    if workflow is not None:
        enum_checks.append(("workflow", workflow, config.get("workflows", [])))
    agents = config.get("agents", {})
    if assignee is not None and agents:
        enum_checks.append(("assignee", assignee, list(agents.keys())))
    for field_name, value, valid in enum_checks:
        err = validate_enum(value, valid, field_name)
        if err:
            errors.append(ImportErrorItem(field_name, err))

    duplicate_policy = item.get("duplicate_policy", "fail")
    if duplicate_policy not in ("fail", "skip", "allow"):
        errors.append(ImportErrorItem("duplicate_policy", "duplicate_policy must be fail, skip, or allow"))
        duplicate_policy = "fail"

    criterion_types = config.get("criterion_types", [])
    for i, tc in enumerate(typed_criteria):
        ct = tc.get("type", "manual")
        spec = tc.get("spec")
        if spec is not None and isinstance(spec, str) and not spec.strip():
            tc["spec"] = None
        if criterion_types and ct not in criterion_types:
            errors.append(ImportErrorItem(f"criteria[{i}].type", f"invalid type '{ct}'"))
        if ct in {"code", "test", "file"} and not tc.get("spec"):
            errors.append(ImportErrorItem(f"criteria[{i}].spec", f"spec required for type '{ct}'"))

    deps, dep_errors = _normalize_dependencies(item.get("depends_on"))
    errors.extend(dep_errors)
    for dep in deps:
        if dep.key is None:
            continue
        field = f"depends_on[{dep.raw_index}]"
        if dep.key not in known_keys:
            errors.append(ImportErrorItem(field, f"unknown task key '{dep.key}'"))
        elif key is not None and dep.key == key:
            errors.append(ImportErrorItem(field, "task cannot depend on itself"))

    metachar_checks = [("task summary", summary), ("task description", description)]
    metachar_checks.extend(("criterion text", text) for text in criteria)
    metachar_checks.extend(("criterion text", tc["text"]) for tc in typed_criteria)
    for subject, text in metachar_checks:
        ok, diagnostic = reject_shell_metacharacters(text, subject=subject)
        if not ok:
            errors.append(ImportErrorItem(subject, diagnostic))

    if errors:
        return None, errors, key

    return TaskPlan(
        index=index,
        key=key,
        summary=summary,
        description=description,
        priority=priority,
        domain=domain,
        task_type=task_type,
        assignee=assignee,
        complexity=complexity,
        workflow=workflow,
        expires_in_days=expires_in_days,
        not_before=not_before,
        fixes_task_id=fixes_task_id,
        scope_patterns=scope_patterns,
        creates_paths=creates_paths,
        unbounded=unbounded,
        criteria=criteria,
        typed_criteria=typed_criteria,
        duplicate_policy=duplicate_policy,
        deps=deps,
    ), [], key


def _task_exists(conn: sqlite3.Connection, task_id: int) -> bool:
    return conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None


def _validate_dependency_references(
    plans: list[TaskPlan],
    conn: sqlite3.Connection,
) -> dict[int, list[ImportErrorItem]]:
    errors: dict[int, list[ImportErrorItem]] = {}
    for plan in plans:
        for dep in plan.deps:
            field = f"depends_on[{dep.raw_index}]"
            if dep.key is not None:
                continue
            elif dep.task_id is not None and not _task_exists(conn, dep.task_id):
                errors.setdefault(plan.index, []).append(
                    ImportErrorItem(field, f"task id {dep.task_id} does not exist")
                )
    return errors


def _materialize_task(
    conn: sqlite3.Connection,
    plan: TaskPlan,
    repo_root: str,
) -> tuple[int, list[int], list[tuple[int, str, str | None]]]:
    inserted = insert_task_record(
        conn,
        summary=plan.summary,
        description=plan.description,
        priority=plan.priority,
        domain=plan.domain,
        task_type=plan.task_type,
        assignee=plan.assignee,
        complexity=plan.complexity,
        workflow=plan.workflow,
        criteria=plan.criteria,
        typed_criteria=plan.typed_criteria,
        repo_root=repo_root,
        expires_in_days=plan.expires_in_days,
        not_before=plan.not_before,
        fixes_task_id=plan.fixes_task_id,
        scope_patterns=plan.scope_patterns,
        creates_paths=plan.creates_paths,
        unbounded=plan.unbounded,
    )
    return inserted.task_id, inserted.criteria_ids, inserted.typed_inserted


def _resolve_dep_id(dep: DependencyRef, key_to_created_id: dict[str, int]) -> int:
    if dep.task_id is not None:
        return dep.task_id
    return key_to_created_id[dep.key or ""]


def _execute_import(
    db_path: str,
    plans: list[TaskPlan],
    result: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
    repo_root: str,
    best_effort: bool,
) -> int:
    if dry_run:
        for plan in plans:
            result["created"][str(plan.index)] = {"dry_run": True}
            if plan.key is not None:
                result["created"][str(plan.index)]["key"] = plan.key
        return 2 if result["failed"] else 0

    typed_inserted: list[tuple[int, str, str | None]] = []
    key_to_created_id: dict[str, int] = {}
    index_to_created_id: dict[int, int] = {}
    conn = get_connection(db_path)
    try:
        for plan in plans:
            try:
                task_id, criteria_ids, typed = _materialize_task(conn, plan, repo_root)
                if best_effort:
                    conn.commit()
                typed_inserted.extend(typed)
                index_to_created_id[plan.index] = task_id
                if plan.key is not None:
                    key_to_created_id[plan.key] = task_id
                entry = {"task_id": task_id, "criteria_ids": criteria_ids}
                if plan.key is not None:
                    entry["key"] = plan.key
                result["created"][str(plan.index)] = entry
            except ValueError as exc:
                conn.rollback()
                result["failed"][str(plan.index)] = _failure_entry(
                    plan.key,
                    [ImportErrorItem("$", str(exc))],
                )
                if not best_effort:
                    result["created"].clear()
                    return 2
            except sqlite3.Error as exc:
                conn.rollback()
                result["failed"][str(plan.index)] = _failure_entry(
                    plan.key,
                    [ImportErrorItem("$", f"database error: {exc}")],
                )
                if not best_effort:
                    result["created"].clear()
                    return 2

        for plan in plans:
            if plan.index not in index_to_created_id:
                continue
            task_id = index_to_created_id[plan.index]
            try:
                for dep in plan.deps:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_dependencies "
                        "(task_id, depends_on_id, relationship_type) VALUES (?, ?, ?)",
                        (task_id, _resolve_dep_id(dep, key_to_created_id), dep.relationship_type),
                    )
                if best_effort:
                    conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                result["failed"][str(plan.index)] = _failure_entry(
                    plan.key,
                    [ImportErrorItem("$", f"database error: {exc}")],
                )
                if not best_effort:
                    result["created"].clear()
                    return 2
        if not best_effort:
            conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        result["failed"]["0"] = _failure_entry(None, [ImportErrorItem("$", f"database error: {exc}")])
        return 2
    finally:
        conn.close()

    _warn_already_passing_criteria(typed_inserted)
    subprocess.run([TUSK_BIN, "wsjf"], capture_output=True)
    return 2 if result["failed"] else 0


def main(argv: list[str]) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="tusk task-import",
        description="Import tasks from a JSON plan",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Read import JSON from a UTF-8 file")
    source.add_argument("--stdin", action="store_true", help="Read import JSON from stdin")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing rows")
    parser.add_argument(
        "--best-effort",
        action="store_true",
        help="Create valid tasks and report per-task failures instead of rolling back the full batch",
    )
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv[2:])

    result = _result_shell()
    loaded, load_errors = _load_json(args)
    if load_errors:
        result["failed"]["0"] = _failure_entry(None, load_errors)
        print(dumps(result))
        return 2
    if not isinstance(loaded, dict):
        result["failed"]["0"] = _failure_entry(None, [ImportErrorItem("$", "top-level JSON must be an object")])
        print(dumps(result))
        return 2
    tasks = loaded.get("tasks")
    if not isinstance(tasks, list):
        result["failed"]["0"] = _failure_entry(None, [ImportErrorItem("tasks", "top-level tasks array is required")])
        print(dumps(result))
        return 2

    config = load_config(config_path)
    repo_root = _repo_root(config_path, args.repo_root)
    plans: list[TaskPlan] = []
    seen_keys: set[str] = set()
    known_keys = {
        item.get("key")
        for item in tasks
        if isinstance(item, dict) and _as_nonempty_string(item.get("key"))
    }
    for index, item in enumerate(tasks):
        plan, errors, key = _validate_item(item, index, config, known_keys)
        if key is not None:
            if key in seen_keys:
                errors.append(ImportErrorItem("key", f"duplicate key '{key}'"))
            seen_keys.add(key)
        if errors:
            result["failed"][str(index)] = _failure_entry(key, errors)
            continue
        plans.append(plan)

    conn = get_connection(db_path)
    try:
        dep_errors = _validate_dependency_references(plans, conn)
    finally:
        conn.close()
    for index, errors in dep_errors.items():
        result["failed"][str(index)] = _failure_entry(
            next((p.key for p in plans if p.index == index), None),
            errors,
        )
    plans = [p for p in plans if str(p.index) not in result["failed"]]

    for plan in list(plans):
        if plan.duplicate_policy == "allow":
            continue
        dupe = run_dupe_check(plan.summary, plan.domain)
        if not dupe:
            continue
        plan.dupe = dupe
        if plan.duplicate_policy == "skip":
            entry = {
                "reason": "duplicate",
                "matched_task_id": dupe.get("id"),
                "matched_summary": dupe.get("summary", ""),
                "similarity": dupe.get("similarity", 0),
            }
            if plan.key is not None:
                entry["key"] = plan.key
            result["skipped"][str(plan.index)] = entry
            plans.remove(plan)
        else:
            result["failed"][str(plan.index)] = _failure_entry(
                plan.key,
                [ImportErrorItem("duplicate_policy", f"duplicate of TASK-{dupe.get('id')}")],
            )
            plans.remove(plan)

    remaining_keys = {plan.key for plan in plans if plan.key is not None}
    skipped_keys = {
        entry.get("key")
        for entry in result["skipped"].values()
        if entry.get("key") is not None
    }
    for plan in list(plans):
        errors: list[ImportErrorItem] = []
        for dep in plan.deps:
            if dep.key is None or dep.key in remaining_keys:
                continue
            field = f"depends_on[{dep.raw_index}]"
            if dep.key in skipped_keys:
                errors.append(ImportErrorItem(field, f"dependency key '{dep.key}' was skipped"))
            else:
                errors.append(ImportErrorItem(field, f"unknown task key '{dep.key}'"))
        if errors:
            result["failed"][str(plan.index)] = _failure_entry(plan.key, errors)
            plans.remove(plan)

    for plan in plans:
        _warn_for_missing_declared_paths(repo_root, plan.scope_patterns, plan.typed_criteria)

    if result["failed"] and not args.best_effort:
        print(dumps(result))
        return 2

    code = _execute_import(
        db_path,
        plans,
        result,
        dry_run=args.dry_run,
        repo_root=repo_root,
        best_effort=args.best_effort,
    )
    print(dumps(result))
    return code


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-import --file tasks.json [--dry-run]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
