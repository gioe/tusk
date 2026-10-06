#!/usr/bin/env python3
"""Compile durable context for picking up a task.

Usage:
    tusk task-brief <task_id> [--format json|markdown]

JSON output shape:
    {
      "task": {...},
      "acceptance_criteria": [{...}],
      "verification_specs": [{"criterion_id": N, "spec": "..."}],
      "scope": [{"id": N, "pattern": "...", "source": "..."}],
      "entry_points": [{...}],
      "dependencies": {"blocked_by": [...], "dependents": [...]},
      "progress": [{...}],
      "objectives": [{...}],
      "context": {
        "memories": [...],
        "assumptions": [...],
        "open_questions": [...],
        "risks": [...],
        "decisions": [...]
      },
      "context_health_warnings": [
        {"code": "missing_entry_points", "message": "...", "details": {...}}
      ]
    }
"""

import argparse
import os
import posixpath
import re
import shlex
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # noqa: E402

_db_lib = tusk_loader.load("tusk-db-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
_is_xctest_selector = _git_helpers.is_xctest_selector


PATH_SUFFIX_RE = re.compile(
    r"\.(py|sh|md|json|toml|yaml|yml|txt|swift|js|jsx|ts|tsx|css|html|sql)$"
)
GLOB_CHARS = frozenset("*?[")
SHELL_EXPANSION_CHARS = frozenset("$`\\~*?[{")
COMMAND_SUB_START = "\0tusk-command-substitution-start\0"
COMMAND_SUB_END = "\0tusk-command-substitution-end\0"


def _task_id_type(value: str) -> int:
    raw = value[5:] if value.upper().startswith("TASK-") else value
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid task ID: {value}") from exc


def _rows(rows) -> list[dict]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _clean_path_token(token: str) -> str | None:
    token = token.strip().lstrip("\"'`,:;()[]{}").rstrip("\"'`.,:;()[]{}")
    if not token or token.startswith("-"):
        return None
    token = token.split("::", 1)[0]
    if token.startswith("./"):
        token = token[2:]
    if token.startswith("/") or ".." in token.split("/"):
        return None
    if "/" not in token and not PATH_SUFFIX_RE.search(token):
        return None
    return token


def _is_command_substitution_start(command: str, index: int) -> bool:
    return command.startswith("$(", index) and not command.startswith("$((", index)


def _command_substitution_end(command: str, start: int) -> int | None:
    """Return the closing-paren index for a command substitution at start."""
    depth = 1
    quote: str | None = None
    index = start + 2
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
            if _is_command_substitution_start(command, index):
                nested_end = _command_substitution_end(command, index)
                if nested_end is None:
                    return None
                index = nested_end + 1
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif _is_command_substitution_start(command, index):
            nested_end = _command_substitution_end(command, index)
            if nested_end is None:
                return None
            index = nested_end + 1
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _command_substitutions(command: str) -> tuple[str, dict[str, str]]:
    """Replace balanced command substitutions with collision-safe markers."""
    marker_prefix = "__TUSK_COMMAND_SUBSTITUTION_"
    while marker_prefix in command:
        marker_prefix += "_"

    substitutions: dict[str, str] = {}
    rendered: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            rendered.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            rendered.append(command[index:index + 2])
            index += 2
            continue
        if quote == '"':
            if char == '"':
                quote = None
                rendered.append(char)
                index += 1
                continue
            if _is_command_substitution_start(command, index):
                end = _command_substitution_end(command, index)
                if end is not None:
                    marker = f"{marker_prefix}{len(substitutions)}__"
                    substitutions[marker] = command[index + 2:end]
                    rendered.append(marker)
                    index = end + 1
                    continue
            rendered.append(char)
            index += 1
            continue
        if char == "'":
            quote = char
            rendered.append(char)
            index += 1
            continue
        if char == '"':
            quote = '"'
            rendered.append(char)
            index += 1
            continue
        if _is_command_substitution_start(command, index):
            end = _command_substitution_end(command, index)
            if end is not None:
                marker = f"{marker_prefix}{len(substitutions)}__"
                substitutions[marker] = command[index + 2:end]
                rendered.append(marker)
                index = end + 1
                continue
        rendered.append(char)
        index += 1
    return "".join(rendered), substitutions


def _shell_scan_tokens(command: str) -> list[str]:
    """Return shell-ish tokens suitable for conservative cwd/path scanning."""
    rendered, substitutions = _command_substitutions(command)
    try:
        lexer = shlex.shlex(rendered, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        outer_tokens = list(lexer)
    except ValueError:
        outer_tokens = rendered.split()

    tokens: list[str] = []
    for token in outer_tokens:
        markers = [
            (token.find(marker), body)
            for marker, body in substitutions.items()
            if marker in token
        ]
        if not markers:
            tokens.append(token)
            continue
        for _, body in sorted(markers):
            tokens.append(COMMAND_SUB_START)
            tokens.extend(_shell_scan_tokens(body))
            tokens.append(COMMAND_SUB_END)
    return tokens


def _is_control_operator(token: str) -> bool:
    return bool(token) and all(char in ";&|" for char in token)


def _resolve_literal_cd(target: str, current_dir: str | None) -> str | None:
    """Resolve a safe repo-relative cd target, or return None when uncertain."""
    target = target.strip().rstrip("/")
    if target.startswith("./"):
        target = target[2:]
    parts = target.split("/")
    if (
        current_dir is None
        or not target
        or target.startswith(("-", "/"))
        or ".." in parts
        or any(char in target for char in SHELL_EXPANSION_CHARS)
    ):
        return None
    return posixpath.normpath(posixpath.join(current_dir, target))


def _spec_paths(spec: str) -> list[str]:
    tokens = _shell_scan_tokens(spec)
    seen: set[str] = set()
    paths: list[str] = []

    current_dir: str | None = ""
    command_base: str | None = current_dir
    and_or_base: str | None = current_dir
    pipeline_base: str | None = None
    in_pipeline = False
    at_command_start = True
    command_name: str | None = None
    python_phase: str | None = None
    substitution_states: list[tuple] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == COMMAND_SUB_START:
            substitution_states.append(
                (
                    current_dir,
                    command_base,
                    and_or_base,
                    pipeline_base,
                    in_pipeline,
                    at_command_start,
                    command_name,
                    # A substitution occupies the pending argument, but its
                    # shell commands still need their own path scan.
                    "options" if python_phase == "option_value" else
                    None if python_phase == "source" else python_phase,
                )
            )
            command_base = current_dir
            and_or_base = current_dir
            pipeline_base = None
            in_pipeline = False
            at_command_start = True
            command_name = None
            python_phase = None
            index += 1
            continue
        if token == COMMAND_SUB_END and substitution_states:
            (
                current_dir,
                command_base,
                and_or_base,
                pipeline_base,
                in_pipeline,
                outer_at_command_start,
                command_name,
                python_phase,
            ) = substitution_states.pop()
            at_command_start = False
            if outer_at_command_start:
                command_name = None
            index += 1
            continue
        if _is_control_operator(token):
            if token in {"|", "|&"}:
                if not in_pipeline:
                    pipeline_base = command_base
                    in_pipeline = True
                current_dir = pipeline_base
            else:
                if in_pipeline:
                    current_dir = pipeline_base
                    in_pipeline = False
                if token == "&":
                    current_dir = and_or_base
                if token in {";", "&"}:
                    and_or_base = current_dir
            command_base = current_dir
            at_command_start = True
            command_name = None
            python_phase = None
            index += 1
            continue

        if at_command_start and token == "cd":
            target = (
                tokens[index + 1]
                if index + 1 < len(tokens)
                and tokens[index + 1] not in {COMMAND_SUB_START, COMMAND_SUB_END}
                and not _is_control_operator(tokens[index + 1])
                else ""
            )
            current_dir = _resolve_literal_cd(target, current_dir)
            if current_dir and current_dir not in seen:
                seen.add(current_dir)
                paths.append(current_dir)
            at_command_start = False
            index += 2 if target else 1
            continue

        is_command_token = at_command_start
        if is_command_token:
            command_name = posixpath.basename(token)
            python_phase = (
                "options" if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", command_name)
                else None
            )
        elif python_phase in {"source", "option_value"}:
            # Inline source is code, not a filesystem operand. Deliberately
            # avoid interpreting or executing Python to infer embedded paths.
            python_phase = "options" if python_phase == "option_value" else None
            index += 1
            continue
        elif python_phase == "options":
            if token == "-c":
                python_phase = "source"
            elif token in {"-W", "-X"}:
                python_phase = "option_value"
            elif token in {"-m", "--", "-"} or not token.startswith("-"):
                python_phase = None
        path = (
            None
            if not is_command_token
            and command_name == "test-sim"
            and _is_xctest_selector(token)
            else _clean_path_token(token)
        )
        if path and current_dir is not None:
            path = posixpath.normpath(posixpath.join(current_dir, path))
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
        at_command_start = False
        index += 1
    return paths


def _scope_is_literal_path(pattern: str) -> bool:
    return bool(pattern) and not any(ch in pattern for ch in GLOB_CHARS)


def _missing_scope_warnings(repo_root: str, scope_rows: list[dict]) -> list[dict]:
    warnings: list[dict] = []
    for row in scope_rows:
        if row["source"] == "unbounded":
            continue
        pattern = row["pattern"]
        if not _scope_is_literal_path(pattern):
            continue
        if not os.path.exists(os.path.join(repo_root, pattern)):
            warnings.append(
                {
                    "code": "missing_scope_path",
                    "message": f"Scope path does not exist: {pattern}",
                    "details": {"scope_id": row["id"], "pattern": pattern},
                }
            )
    return warnings


def _stale_spec_warnings(repo_root: str, criteria_rows: list[dict]) -> list[dict]:
    warnings: list[dict] = []
    for row in criteria_rows:
        spec = row.get("verification_spec")
        if not spec:
            continue
        missing = [
            path for path in _spec_paths(spec) if not os.path.exists(os.path.join(repo_root, path))
        ]
        if missing:
            warnings.append(
                {
                    "code": "stale_verification_spec",
                    "message": f"Verification spec references missing path(s): {', '.join(missing)}",
                    "details": {"criterion_id": row["id"], "missing_paths": missing},
                }
            )
    return warnings


def _context_sections(context_items: list[dict]) -> dict:
    sections = {
        "memories": [],
        "assumptions": [],
        "open_questions": [],
        "risks": [],
        "decisions": [],
    }
    mapping = {
        "memory": "memories",
        "assumption": "assumptions",
        "question": "open_questions",
        "risk": "risks",
        "decision": "decisions",
    }
    for item in context_items:
        key = mapping.get(item["item_type"])
        if key:
            sections[key].append(item)
    return sections


def provenance_slice(conn, task_id, repo_root, budget):
    """Select a small current packet, never recursively load a history graph."""
    import json
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    packet = {'budget_chars': budget, 'items': [], 'omitted_refs': [],
              'truncated': False, 'selection_truncated': False,
              'omitted_refs_truncated': False, 'scope': 'direct_task_context',
              'task_ref': None}
    if 'provenance_records' not in tables:
        packet['availability'] = 'legacy_schema'
        return packet
    p = tusk_loader.load('tusk-provenance')
    evidence = tusk_loader.load('tusk-evidence-lib')
    project = p.project_id(conn)

    def ref(kind, native_id):
        row = conn.execute("SELECT id FROM provenance_records WHERE kind=? AND native_id=? AND availability='available'", (kind, native_id)).fetchone()
        return p.reference(project, row[0]) if row else None

    def rows(sql, args=()):
        found = conn.execute(sql + ' LIMIT 21', args).fetchall()
        if len(found) > 20:
            packet['selection_truncated'] = True
        return found[:20]

    candidates = []
    packet['task_ref'] = ref('task', task_id)
    criteria = rows('SELECT id,criterion,criterion_type,verification_spec,is_completed FROM acceptance_criteria WHERE task_id=? ORDER BY is_completed,id', (task_id,))
    atoms = rows("SELECT id,item_type,content,status FROM task_context_items WHERE task_id=? AND status='active' AND item_type IN ('decision','question','assumption') ORDER BY id DESC", (task_id,))
    roots = [packet['task_ref']] + [ref('criterion', r['id']) for r in criteria] + [ref('context', r['id']) for r in atoms]
    roots = [r.rsplit(':', 1)[1] for r in roots if r]
    if roots:
        placeholders = ','.join('?' for _ in roots)
        sources = rows(f"SELECT DISTINCT r.* FROM provenance_links l JOIN provenance_records r ON r.id=l.target_id WHERE l.source_id IN ({placeholders}) AND l.relationship IN ('derived_from','responds_to') AND r.kind='prompt' ORDER BY r.id", roots)
        for source in sources:
            source_ref = p.reference(project, source['id'])
            replacement = conn.execute("SELECT source_id FROM provenance_links WHERE target_id=? AND relationship='supersedes' ORDER BY id DESC LIMIT 1", (source['id'],)).fetchone()
            snapshot = p.prompt_snapshot(conn, source['id']) if source['availability'] == 'available' else None
            candidates.append({'type': 'source_intent', 'ref': source_ref,
                               'state': 'superseded' if replacement else 'available' if snapshot else 'unknown',
                               'text': snapshot['content'] if snapshot and not replacement else None,
                               'representation': snapshot['representation'] if snapshot else None,
                               'source_truncated': snapshot['truncated'] if snapshot else False})
    for criterion in criteria:
        if not criterion['is_completed']:
            candidates.append({'type': 'outstanding_promise', 'ref': ref('criterion', criterion['id']),
                               'native_id': criterion['id'], 'text': criterion['criterion']})
    for atom in atoms:
        candidates.append({'type': atom['item_type'], 'ref': ref('context', atom['id']),
                           'native_id': atom['id'], 'text': atom['content']})
    progress = conn.execute("SELECT id,next_steps FROM task_progress WHERE task_id=? AND next_steps IS NOT NULL AND trim(next_steps) <> '' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if progress:
        candidates.append({'type': 'next_steps', 'ref': ref('progress', progress['id']),
                           'native_id': progress['id'], 'text': progress['next_steps']})
    if evidence.enabled(conn):
        # A pinned project from another repository cannot validate this checkout.
        try:
            same_project = evidence.git(os.getcwd(), 'rev-parse', '--path-format=absolute', '--git-common-dir') == evidence.git(repo_root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        except (OSError, ValueError):
            same_project = False
        current = evidence.snapshot() if same_project else {'state': 'unknown'}
        for criterion in criteria:
            criterion_ref = ref('criterion', criterion['id'])
            if not criterion_ref:
                continue
            attempt = conn.execute('SELECT r.* FROM provenance_attempts a JOIN provenance_records r ON r.id=a.record_id WHERE a.criterion_id=? ORDER BY a.rowid DESC LIMIT 1', (criterion_ref.rsplit(':', 1)[1],)).fetchone()
            if not attempt:
                continue
            proof = evidence.output(conn, attempt)
            target = proof['details'].get('target', {})
            final = proof['result']
            captured = proof['details'].get('criterion_snapshot', {})
            same_promise = all(captured.get(k) == criterion[k] for k in ('criterion','criterion_type','verification_spec'))
            state = 'unknown'
            if final is None:
                state = 'pending'
            elif final['automated']:
                artifact = p.fetch_record(conn, proof['artifact_ref']) if proof['artifact_ref'] else None
                available = attempt['availability'] == 'available' and artifact is not None and artifact['availability'] == 'available'
                if available and current.get('digest') and target.get('digest'):
                    state = 'current' if same_promise and current['digest'] == target['digest'] and current['head'] == target.get('head') else 'stale'
            else:
                state = final['outcome']
            candidates.append({'type': 'evidence', 'ref': p.reference(project, attempt['id']),
                               'criterion_ref': criterion_ref, 'artifact_ref': proof['artifact_ref'],
                               'state': state, 'automated_current': state == 'current',
                               'checked_head': target.get('head'), 'checked_digest': target.get('digest'),
                               'mode': proof['mode']})
    historical = rows("SELECT r.id FROM task_context_items c JOIN provenance_records r ON r.native_id=c.id AND r.kind='context' WHERE c.task_id=? AND c.status <> 'active' ORDER BY c.id DESC", (task_id,))
    for row in historical:
        candidates.append({'type': 'historical_context', 'ref': p.reference(project, row['id'])})

    # Budget measures the compact serialized provenance object, not legacy fields.
    def size():
        return len(json.dumps(packet, ensure_ascii=True, separators=(',', ':')))

    for candidate in candidates:
        text = candidate.get('text')
        if text and len(text) > 600:
            candidate = dict(candidate, text=text[:600], content_truncated=True)
            packet['truncated'] = True
        packet['items'].append(candidate)
        if size() > budget - 300:  # reserve room for omission metadata
            packet['items'].pop()
            packet['truncated'] = True
            omitted = candidate.get('ref') or f"{candidate['type']}:{candidate.get('native_id', 'unknown')}"
            if len(packet['omitted_refs']) < 10:
                packet['omitted_refs'].append(omitted)
                if size() > budget:
                    packet['omitted_refs'].pop()
                    packet['omitted_refs_truncated'] = True
            else:
                packet['omitted_refs_truncated'] = True
    packet['truncated'] = packet['truncated'] or packet['selection_truncated']
    return packet


def build_brief(conn: sqlite3.Connection, task_id: int, repo_root: str, provenance_budget: int = 6000) -> dict | None:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return None

    criteria = _rows(
        conn.execute(
            "SELECT id, task_id, criterion, source, is_completed, criterion_type, "
            "verification_spec, skip_note, created_at, updated_at "
            "FROM acceptance_criteria WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    )
    progress = _rows(
        conn.execute(
            "SELECT * FROM task_progress WHERE task_id = ? ORDER BY created_at DESC, id DESC",
            (task_id,),
        ).fetchall()
    )
    scope = _rows(
        conn.execute(
            "SELECT id, task_id, pattern, source, reason, locked_at, locked_by, created_at "
            "FROM task_scope WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    )
    context_items = _rows(
        conn.execute(
            "SELECT id, task_id, objective_id, item_type, content, status, source, "
            "created_at, updated_at, resolved_at "
            "FROM task_context_items WHERE task_id = ? AND status = 'active' "
            "ORDER BY item_type, id",
            (task_id,),
        ).fetchall()
    )
    entry_points = [item for item in context_items if item["item_type"] == "entry_point"]

    blocked_by = _rows(
        conn.execute(
            "SELECT d.depends_on_id AS task_id, d.relationship_type, t.summary, t.status "
            "FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_id "
            "WHERE d.task_id = ? ORDER BY d.relationship_type, d.depends_on_id",
            (task_id,),
        ).fetchall()
    )
    dependents = _rows(
        conn.execute(
            "SELECT d.task_id, d.relationship_type, t.summary, t.status "
            "FROM task_dependencies d JOIN tasks t ON t.id = d.task_id "
            "WHERE d.depends_on_id = ? ORDER BY d.relationship_type, d.task_id",
            (task_id,),
        ).fetchall()
    )
    objectives = _rows(
        conn.execute(
            "SELECT o.id, o.summary, o.description, o.status, ot.relationship_type, "
            "ot.created_at AS linked_at "
            "FROM objective_tasks ot JOIN objectives o ON o.id = ot.objective_id "
            "WHERE ot.task_id = ? ORDER BY o.status, o.id",
            (task_id,),
        ).fetchall()
    )

    verification_specs = [
        {
            "criterion_id": row["id"],
            "criterion": row["criterion"],
            "type": row["criterion_type"],
            "spec": row["verification_spec"],
        }
        for row in criteria
        if row.get("verification_spec")
    ]

    warnings = []
    if not entry_points:
        warnings.append(
            {
                "code": "missing_entry_points",
                "message": "No active entry_point context items are attached to this task.",
                "details": {"task_id": task_id},
            }
        )
    warnings.extend(_missing_scope_warnings(repo_root, scope))
    warnings.extend(_stale_spec_warnings(repo_root, criteria))

    return {
        "task": {key: task[key] for key in task.keys()},
        "acceptance_criteria": criteria,
        "verification_specs": verification_specs,
        "scope": scope,
        "entry_points": entry_points,
        "dependencies": {"blocked_by": blocked_by, "dependents": dependents},
        "progress": progress,
        "objectives": objectives,
        "context": _context_sections(context_items),
        "context_health_warnings": warnings,
        "provenance": provenance_slice(conn, task_id, repo_root, provenance_budget),
    }


def _markdown_list(items: list[str]) -> str:
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)


def _provenance_markdown(packet: dict) -> str:
    lines = []
    for item in packet.get("items", []):
        detail = item.get("text") or item.get("state") or "historical reference"
        label = item.get("ref") or str(item.get("native_id", "unknown"))
        lines.append(f"{item['type']}: {detail} ({label})")
        if item.get("artifact_ref"):
            lines.append(f"Checked artifact: {item['artifact_ref']}; revision: {item.get('checked_head') or 'unknown'}")
        if item.get("content_truncated"):
            lines.append("Content truncated; inspect the reference for the full record.")
    if packet.get("truncated"):
        lines.append("Provenance truncated; use trace and context/criteria/progress reads for omitted history.")
    if packet.get("omitted_refs"):
        lines.append("Omitted: " + ", ".join(packet["omitted_refs"]))
    return _markdown_list(lines)


def render_markdown(brief: dict) -> str:
    task = brief["task"]
    criteria = [
        f"[{'x' if row['is_completed'] else ' '}] {row['criterion']}"
        for row in brief["acceptance_criteria"]
    ]
    specs = [
        f"Criterion {row['criterion_id']}: `{row['spec']}`"
        for row in brief["verification_specs"]
    ]
    scope = [
        f"{row['pattern']} ({row['source']})"
        for row in brief["scope"]
    ]
    entry_points = [row["content"] for row in brief["entry_points"]]
    warnings = [
        f"{row['code']}: {row['message']}"
        for row in brief["context_health_warnings"]
    ]
    progress = []
    for row in brief["progress"][:5]:
        text = row.get("next_steps") or row.get("note") or row.get("commit_message")
        if text:
            progress.append(text)

    return "\n".join(
        [
            f"# TASK-{task['id']}: {task['summary']}",
            "",
            f"Status: {task['status']} | Priority: {task['priority']} | Complexity: {task['complexity']}",
            "",
            "## Description",
            task.get("description") or "None",
            "",
            "## Criteria",
            _markdown_list(criteria),
            "",
            "## Verification",
            _markdown_list(specs),
            "",
            "## Scope",
            _markdown_list(scope),
            "",
            "## Entry Points",
            _markdown_list(entry_points),
            "",
            "## Recent Progress",
            _markdown_list(progress),
            "",
            "## Current Provenance",
            _provenance_markdown(brief.get("provenance", {})),
            "",
            "## Context Health",
            _markdown_list(warnings),
        ]
    )


def main(argv: list[str]) -> int:
    db_path = argv[0]
    # argv[1] is config_path (unused)
    repo_root = argv[2]

    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk task-brief",
        description="Compile durable task context for a fresh session.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("task_id", type=_task_id_type, help="Task ID (integer or TASK-NNN form)")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        dest="fmt",
        help="Output format. json returns the compiled context packet; markdown renders a concise pickup brief.",
    )
    parser.add_argument("--provenance-budget", type=int, default=6000,
                        help="Compact provenance JSON character budget, 2000..32000; legacy fields unchanged.")
    args = parser.parse_args(argv[3:])
    if not 2000 <= args.provenance_budget <= 32000:
        parser.error("provenance budget must be 2000..32000")

    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        brief = build_brief(conn, args.task_id, repo_root, args.provenance_budget)
        if brief is None:
            print(f"Error: Task {args.task_id} not found", file=sys.stderr)
            return 1
        if args.fmt == "markdown":
            print(render_markdown(brief))
        else:
            print(dumps(brief))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 3 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-brief <task_id> [--format json|markdown]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
