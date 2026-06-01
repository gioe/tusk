"""Integration tests for the auto-extract and scope-hint surfaces that
``/create-task`` relies on (TASK-475).

Two behaviors are under test:

1. ``tusk task-insert`` auto-extracts file paths from the description and
   criterion specs at insert time and records them in ``task_scope`` as
   ``source='auto_derived'`` — so /create-task does not have to enumerate
   every path the prose already names (criterion 2200).

2. ``tusk scope-hint`` emits structured suggestions
   (``scope``/``creates``/``unbounded``) that /create-task surfaces to
   the operator during Step 3 (criteria 2201, 2202).

Both surfaces are read-only at the LLM boundary — they exist so the skill
prose can stay deterministic instead of relying on an LLM to spot every
path mention.
"""

import json
import os
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(args, env=None):
    return subprocess.run(
        [TUSK_BIN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _scope_rows(db: str, task_id: int) -> list:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pattern, source FROM task_scope WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _insert(db_path, summary, description, **kw):
    """Run `tusk task-insert` and return the new task_id."""
    args = ["task-insert", summary, description, "--criteria", "marker"]
    for spec in kw.pop("typed_criteria", []):
        args.extend(["--typed-criteria", spec])
    for s in kw.pop("scope", []):
        args.extend(["--scope", s])
    for c in kw.pop("creates", []):
        args.extend(["--creates", c])
    if kw.pop("unbounded", False):
        args.append("--unbounded")
    assert not kw, f"unexpected kwargs: {kw}"
    result = _run(args)
    assert result.returncode == 0, (
        f"task-insert failed: rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return json.loads(result.stdout)["task_id"]


# ── task-insert missing-path warnings (TASK-554) ────────────────────────


def test_task_insert_warns_for_missing_scope_path(db_path):
    """A missing non-glob ``--scope`` path warns without blocking insert."""
    result = _run([
        "task-insert",
        "missing scope warning",
        "body",
        "--scope",
        "this/path/does/not/exist,bin/tusk-task-insert.py",
        "--criteria",
        "marker",
    ])

    assert result.returncode == 0, result.stderr
    assert "Warning:" in result.stderr
    assert "this/path/does/not/exist" in result.stderr

    task_id = json.loads(result.stdout)["task_id"]
    rows = _scope_rows(str(db_path), task_id)
    declared = {r["pattern"] for r in rows if r["source"] == "operator_declared"}
    assert "this/path/does/not/exist" in declared
    assert "bin/tusk-task-insert.py" in declared


def test_task_insert_does_not_warn_for_existing_scope_path(db_path):
    result = _run([
        "task-insert",
        "existing scope path",
        "body",
        "--scope",
        "bin/tusk-task-insert.py",
        "--criteria",
        "marker",
    ])

    assert result.returncode == 0, result.stderr
    assert "Warning:" not in result.stderr


def test_task_insert_does_not_warn_for_glob_scope_pattern(db_path):
    result = _run([
        "task-insert",
        "glob scope path",
        "body",
        "--scope",
        "tests/integration/*.missing",
        "--criteria",
        "marker",
    ])

    assert result.returncode == 0, result.stderr
    assert "Warning:" not in result.stderr


def test_task_insert_warns_for_missing_verification_spec_path(db_path):
    result = _run([
        "task-insert",
        "missing spec path",
        "body",
        "--criteria",
        "marker",
        "--typed-criteria",
        json.dumps({
            "text": "Spec path check",
            "type": "test",
            "spec": (
                "grep -R needle apps/web/ui/pages/show 2>/dev/null || "
                "python3 -m pytest tests/integration/test_create_task_scope.py -q"
            ),
        }),
    ])

    assert result.returncode == 0, result.stderr
    assert "Warning:" in result.stderr
    assert "apps/web/ui/pages/show" in result.stderr
    assert "tests/integration/test_create_task_scope.py" not in result.stderr


# ── task-insert auto-extraction (criterion 2200) ────────────────────────


def test_auto_extract_from_criteria(db_path):
    """Paths inside typed-criteria ``spec`` text become ``auto_derived``
    rows on ``task_scope`` at insert time — without the operator having
    to repeat them via ``--scope``."""
    task_id = _insert(
        str(db_path),
        "no paths in summary",
        "no paths in description either",
        typed_criteria=[
            json.dumps({
                "text": "Migration test passes",
                "type": "test",
                "spec": "python3 -m pytest tests/integration/test_create_task_scope.py -q",
            }),
            json.dumps({
                "text": "Helper exists",
                "type": "file",
                "spec": "bin/tusk-scope-hint.py",
            }),
        ],
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_create_task_scope.py" in auto, (
        f"test-criterion path should be auto-extracted: {rows}"
    )
    assert "bin/tusk-scope-hint.py" in auto, (
        f"file-criterion spec path should be auto-extracted: {rows}"
    )


def test_auto_extract_prefixes_relative_paths_after_cd_and(db_path):
    """A leading ``cd <dir> &&`` makes later spec paths relative to that dir."""
    task_id = _insert(
        str(db_path),
        "cd spec path",
        "no paths in description either",
        typed_criteria=[
            json.dumps({
                "text": "Route test passes",
                "type": "test",
                "spec": "cd apps/web && npx vitest run app/api/health/route.test.ts",
            }),
        ],
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "apps/web/app/api/health/route.test.ts" in auto, rows
    assert "app/api/health/route.test.ts" not in auto, rows


def test_auto_extract_prefixes_relative_paths_after_cd_semicolon(db_path):
    """The same path prefixing applies to ``cd <dir>;`` command sequences."""
    task_id = _insert(
        str(db_path),
        "semicolon cd spec path",
        "no paths in description either",
        typed_criteria=[
            json.dumps({
                "text": "Route test passes",
                "type": "test",
                "spec": "cd apps/web; npx vitest run app/api/health/route.test.ts",
            }),
        ],
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "apps/web/app/api/health/route.test.ts" in auto, rows
    assert "app/api/health/route.test.ts" not in auto, rows


def test_auto_extract_skipped_when_unbounded(db_path):
    """``--unbounded`` opts the task out of path restriction entirely —
    no ``auto_derived`` rows should land even when the description names
    paths."""
    task_id = _insert(
        str(db_path),
        "unbounded refactor",
        "touches bin/foo.py and skills/bar/SKILL.md",
        unbounded=True,
    )

    rows = _scope_rows(str(db_path), task_id)
    sources = {r["source"] for r in rows}
    assert sources == {"unbounded"}, (
        f"unbounded task should have only the sentinel row, got: {rows}"
    )


def test_auto_extract_dedups_against_explicit_scope(db_path):
    """Paths declared via ``--scope`` or ``--creates`` should not also be
    inserted as ``auto_derived`` rows."""
    task_id = _insert(
        str(db_path),
        "explicit overlap",
        "touches bin/foo.py and also bin/bar.py",
        scope=["bin/foo.py"],
        creates=["bin/baz.py"],
    )

    rows = _scope_rows(str(db_path), task_id)
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], set()).add(r["pattern"])

    # bin/foo.py named in description AND --scope → operator_declared, no
    # duplicate auto_derived row.
    assert by_source.get("operator_declared") == {"bin/foo.py"}, rows
    assert "bin/foo.py" not in by_source.get("auto_derived", set())
    # bin/bar.py named in description only → auto_derived.
    assert "bin/bar.py" in by_source.get("auto_derived", set()), rows
    # bin/baz.py via --creates is a creates row, not auto_derived.
    assert by_source.get("creates") == {"bin/baz.py"}, rows


def test_auto_extract_dedups_prefixed_spec_against_explicit_scope(db_path):
    """A cd-relative spec path should not duplicate its declared root path."""
    task_id = _insert(
        str(db_path),
        "explicit prefixed overlap",
        "no paths in description either",
        scope=["apps/web/app/api/health/route.test.ts"],
        typed_criteria=[
            json.dumps({
                "text": "Route test passes",
                "type": "test",
                "spec": "cd apps/web && npx vitest run app/api/health/route.test.ts",
            }),
        ],
    )

    rows = _scope_rows(str(db_path), task_id)
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], set()).add(r["pattern"])

    assert by_source.get("operator_declared") == {
        "apps/web/app/api/health/route.test.ts"
    }, rows
    assert "apps/web/app/api/health/route.test.ts" not in by_source.get(
        "auto_derived", set()
    )


def test_auto_extract_resolves_unique_suffix_match(db_path):
    """A project-relative path resolves when exactly one tracked file has that suffix."""
    task_id = _insert(
        str(db_path),
        "suffix scope",
        "Touch integration/test_create_task_scope.py during the fix.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_create_task_scope.py" in auto, rows
    assert "integration/test_create_task_scope.py" not in auto, rows


def test_auto_extract_keeps_missing_suffix_literal(db_path):
    """A missing path with no suffix match keeps the extracted literal."""
    task_id = _insert(
        str(db_path),
        "missing suffix scope",
        "Touch tests/no/such_scope_suffix_file.py during the fix.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/no/such_scope_suffix_file.py" in auto, rows


def test_auto_extract_resolves_pytest_nodeid_to_file_path(db_path):
    """Pytest nodeids resolve by their file portion and store the file path."""
    task_id = _insert(
        str(db_path),
        "nodeid suffix scope",
        (
            "Run integration/test_create_task_scope.py::"
            "test_auto_extract_from_criteria after the fix."
        ),
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_create_task_scope.py" in auto, rows
    assert not any("::" in p for p in auto), rows


def test_auto_extract_splits_bare_toplevel_files_joined_by_slash(db_path):
    """Prose like ``VERSION/CHANGELOG.md`` names two top-level files, not a path."""
    task_id = _insert(
        str(db_path),
        "version docs",
        "tusk version-bump and tusk changelog-add update VERSION/CHANGELOG.md together.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "VERSION" in auto, rows
    assert "CHANGELOG.md" in auto, rows
    assert "VERSION/CHANGELOG.md" not in auto, rows


def test_auto_extract_rejects_prose_identifier_tokens(db_path):
    """Method-call prose such as ``console.error/console.log`` is not a path."""
    task_id = _insert(
        str(db_path),
        "logging cleanup",
        "Investigate console.error/console.log usage in the failing Vitest run.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "console.error/console.log" not in auto, rows
    assert "console.error" not in auto, rows


# ── scope-hint creates suggestion (criterion 2201) ──────────────────────


def test_creates_suggestion(db_path):
    """``tusk scope-hint`` suggests a ``creates`` entry when the
    description explicitly says a new file is being added."""
    result = _run([
        "scope-hint",
        "--summary", "Add helper script",
        "--description", "Create a new file bin/tusk-foo.py that does X.",
        "--task-type", "feature",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert "bin/tusk-foo.py" in payload["creates"], payload
    assert "bin/tusk-foo.py" in payload["scope"], payload
    assert payload["unbounded"] is False, payload
    assert "creates" in payload["rationale"], payload


def test_creates_suggestion_phrase_variants(db_path):
    """Both ``create a new …`` and bare ``new file …`` patterns fire."""
    # "add a new test file <path>"
    result = _run([
        "scope-hint",
        "--summary", "Cover X",
        "--description", "Add a new test tests/integration/test_x.py covering the new path.",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "tests/integration/test_x.py" in payload["creates"], payload

    # "new script <path>" without an explicit verb
    result = _run([
        "scope-hint",
        "--summary", "Add helper",
        "--description", "Introduces new script bin/helper.py used by /foo.",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "bin/helper.py" in payload["creates"], payload


def test_creates_suggestion_skips_plain_mention(db_path):
    """A path that's merely mentioned (no creation verb) goes into
    ``scope`` but not ``creates``."""
    result = _run([
        "scope-hint",
        "--summary", "Touch existing file",
        "--description", "Edit bin/tusk-task-insert.py to do X.",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert "bin/tusk-task-insert.py" in payload["scope"], payload
    assert payload["creates"] == [], payload


def test_scope_hint_includes_extensionless_bin_tusk_reproducer(db_path):
    """Issue #942: explicitly named extensionless scripts must not be dropped."""
    result = _run([
        "scope-hint",
        "--description",
        (
            "Mitigation: bin/tusk emits a one-line stderr advisory when invoked. "
            "PATH-resolved tusk loaded bin/tusk-review.py."
        ),
        "--task-type",
        "bug",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert "bin/tusk" in payload["scope"], payload
    assert "bin/tusk-review.py" in payload["scope"], payload


# ── scope-hint unbounded suggestion (criterion 2202) ────────────────────


def test_unbounded_suggestion(db_path):
    """``tusk scope-hint`` flags ``--task-type=refactor`` as unbounded
    because refactors typically span many files."""
    result = _run([
        "scope-hint",
        "--summary", "Rename X to Y",
        "--description", "Move every reference to X.",
        "--task-type", "refactor",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["unbounded"] is True, payload
    assert "refactor" in payload["rationale"].get("unbounded", ""), payload


def test_unbounded_suggestion_from_signal_phrase(db_path):
    """Cross-cutting signal phrases trip ``unbounded`` even for a
    non-refactor task type."""
    result = _run([
        "scope-hint",
        "--summary", "Update docstrings",
        "--description", "Sweep through every skill and reword the header.",
        "--task-type", "docs",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["unbounded"] is True, payload
    assert "signal phrase" in payload["rationale"].get("unbounded", ""), payload


def test_unbounded_suggestion_off_for_normal_feature(db_path):
    """A scoped feature task is not flagged as unbounded."""
    result = _run([
        "scope-hint",
        "--summary", "Add login endpoint",
        "--description", "POST /auth/login returns a JWT.",
        "--task-type", "feature",
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["unbounded"] is False, payload
