import json
import os
import sqlite3
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _init_db(tmp_path, monkeypatch):
    db_path = tmp_path / "tusk" / "tasks.db"
    monkeypatch.setenv("TUSK_DB", str(db_path))
    result = subprocess.run(
        [TUSK_BIN, "init", "--force", "--skip-gitignore"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return db_path


def _run_brief(tmp_path, db_path, *args):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return subprocess.run(
        [TUSK_BIN, "task-brief", *[str(arg) for arg in args]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _insert_task_bundle(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score) VALUES (?, ?, 'In Progress', 'feature', "
            "'High', 'M', 27)",
            ("Add task brief", "Compile durable context for pickup."),
        )
        task_id = cur.lastrowid
        dep_id = conn.execute(
            "INSERT INTO tasks (summary, status, task_type, priority, complexity, priority_score) "
            "VALUES ('Prereq', 'To Do', 'bug', 'Medium', 'S', 10)"
        ).lastrowid
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_id, relationship_type) "
            "VALUES (?, ?, 'blocks')",
            (task_id, dep_id),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, criterion_type, verification_spec) "
            "VALUES (?, 'JSON packet exists', 'test', ?)",
            (task_id, "python3 -m pytest tests/integration/test_task_brief.py -q"),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, criterion_type, verification_spec) "
            "VALUES (?, 'Stale spec is warned', 'test', ?)",
            (task_id, "python3 -m pytest tests/missing/test_gone.py -q"),
        )
        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source, reason) "
            "VALUES (?, 'bin/tusk-task-brief.py', 'operator_declared', 'new command')",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_scope (task_id, pattern, source, reason) "
            "VALUES (?, 'docs/missing-task-brief.md', 'operator_declared', 'doc pointer')",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_context_items (task_id, item_type, content, source) "
            "VALUES (?, 'assumption', 'Use durable DB context only.', 'manual')",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_context_items (task_id, item_type, content, source) "
            "VALUES (?, 'risk', 'Warnings may get noisy.', 'manual')",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_progress (task_id, note, next_steps) "
            "VALUES (?, 'Started implementation', 'Wire dispatcher')",
            (task_id,),
        )
        obj_id = conn.execute(
            "INSERT INTO objectives (summary, description) VALUES ('Improve handoff', 'Fresh sessions need context')"
        ).lastrowid
        conn.execute(
            "INSERT INTO objective_tasks (objective_id, task_id, relationship_type) "
            "VALUES (?, ?, 'primary')",
            (obj_id, task_id),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _insert_verification_spec(db_path, task_id, criterion, spec):
    conn = sqlite3.connect(db_path)
    try:
        criterion_id = conn.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, criterion, criterion_type, verification_spec) "
            "VALUES (?, ?, 'test', ?)",
            (task_id, criterion, spec),
        ).lastrowid
        conn.commit()
        return criterion_id
    finally:
        conn.close()


def _materialize_valid_paths(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tusk-task-brief.py").write_text("# helper\n", encoding="utf-8")
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "tests" / "integration" / "test_task_brief.py").write_text(
        "# test\n",
        encoding="utf-8",
    )


def _materialize_web_paths(tmp_path):
    vitest = tmp_path / "apps" / "web" / "node_modules" / ".bin" / "vitest"
    vitest.parent.mkdir(parents=True)
    vitest.write_text("#!/bin/sh\n", encoding="utf-8")
    route_test = tmp_path / "apps" / "web" / "app" / "api" / "health" / "route.test.ts"
    route_test.parent.mkdir(parents=True)
    route_test.write_text("// test\n", encoding="utf-8")


def test_task_brief_json_returns_compiled_context_packet(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _materialize_valid_paths(tmp_path)
    task_id = _insert_task_bundle(db_path)

    result = _run_brief(tmp_path, db_path, task_id, "--format", "json")

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["task"]["id"] == task_id
    assert data["verification_specs"][0]["spec"].startswith("python3 -m pytest")
    assert data["scope"][0]["pattern"] == "bin/tusk-task-brief.py"
    assert data["dependencies"]["blocked_by"][0]["summary"] == "Prereq"
    assert data["progress"][0]["next_steps"] == "Wire dispatcher"
    assert data["objectives"][0]["summary"] == "Improve handoff"
    assert data["context"]["assumptions"][0]["content"] == "Use durable DB context only."
    assert data["context"]["risks"][0]["content"] == "Warnings may get noisy."


def test_task_brief_markdown_renders_concise_pickup_brief(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _materialize_valid_paths(tmp_path)
    task_id = _insert_task_bundle(db_path)

    result = _run_brief(tmp_path, db_path, f"TASK-{task_id}", "--format", "markdown")

    assert result.returncode == 0, result.stderr
    assert f"# TASK-{task_id}: Add task brief" in result.stdout
    assert "## Criteria" in result.stdout
    assert "## Verification" in result.stdout
    assert "## Context Health" in result.stdout


def test_task_brief_context_health_warnings(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _materialize_valid_paths(tmp_path)
    task_id = _insert_task_bundle(db_path)

    result = _run_brief(tmp_path, db_path, task_id, "--format", "json")

    assert result.returncode == 0, result.stderr
    warnings = json.loads(result.stdout)["context_health_warnings"]
    codes = {warning["code"] for warning in warnings}
    assert {
        "missing_entry_points",
        "missing_scope_path",
        "stale_verification_spec",
    }.issubset(codes)
    stale = next(w for w in warnings if w["code"] == "stale_verification_spec")
    assert stale["details"]["missing_paths"] == ["tests/missing/test_gone.py"]


def test_task_brief_resolves_leading_cd_paths_from_project_root(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    _materialize_valid_paths(tmp_path)
    _materialize_web_paths(tmp_path)
    (tmp_path / ".git").mkdir()
    task_id = _insert_task_bundle(db_path)
    valid_id = _insert_verification_spec(
        db_path,
        task_id,
        "Valid web route spec",
        "cd apps/web && node_modules/.bin/vitest run app/api/health/route.test.ts",
    )
    missing_id = _insert_verification_spec(
        db_path,
        task_id,
        "Missing web route spec",
        "cd apps/web; node_modules/.bin/vitest run app/api/health/missing.test.ts",
    )

    root_result = _run_brief(tmp_path, db_path, task_id, "--format", "json")
    nested_result = _run_brief(tmp_path / "apps", db_path, task_id, "--format", "json")

    assert root_result.returncode == 0, root_result.stderr
    assert nested_result.returncode == 0, nested_result.stderr
    root_warnings = json.loads(root_result.stdout)["context_health_warnings"]
    nested_warnings = json.loads(nested_result.stdout)["context_health_warnings"]
    assert nested_warnings == root_warnings
    stale_by_criterion = {
        warning["details"]["criterion_id"]: warning
        for warning in root_warnings
        if warning["code"] == "stale_verification_spec"
    }
    assert valid_id not in stale_by_criterion
    assert stale_by_criterion[missing_id]["details"]["missing_paths"] == [
        "apps/web/app/api/health/missing.test.ts"
    ]


def test_task_brief_help_documents_json_format():
    result = subprocess.run(
        [TUSK_BIN, "task-brief", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert "json returns the compiled context packet" in result.stdout
    assert "--format" in result.stdout
