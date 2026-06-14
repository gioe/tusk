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
    repo_root = kw.pop("repo_root", None)
    if repo_root is not None:
        args.extend(["--repo-root", str(repo_root)])
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


def _git(cwd, args):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result


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


def test_task_insert_auto_extracts_bare_github_subdirectories(db_path):
    task_id = _insert(
        str(db_path),
        "bare github directory scope",
        "Sweep .github/workflows/ and .github/actions/ for Node 24 support.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}
    assert ".github/workflows/**" in auto
    assert ".github/actions/**" in auto


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


def test_auto_extract_keeps_bracketed_dynamic_route_path(db_path):
    """Next.js dynamic route segments are valid path tokens."""
    task_id = _insert(
        str(db_path),
        "dynamic route scope",
        "Touch apps/web/app/admin/clubs/[id]/page.tsx during the fix.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "apps/web/app/admin/clubs/[id]/page.tsx" in auto, rows


def test_auto_extract_resolves_route_only_unique_suffix(db_path, tmp_path):
    """Route-only paths resolve when they uniquely name a tracked file suffix."""
    repo = tmp_path / "next-app"
    repo.mkdir()
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@example.com"])
    _git(repo, ["config", "user.name", "Test User"])
    paths = [
        "apps/web/app/admin/page.tsx",
        "apps/web/app/admin/overview/page.tsx",
    ]
    for path in paths:
        full_path = repo / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(f"// {path}\n", encoding="utf-8")
    _git(repo, ["add", *paths])
    _git(repo, ["commit", "-m", "seed routes"])

    task_id = _insert(
        str(db_path),
        "partial route scope",
        "Touch /admin/page.tsx and /admin/overview/page.tsx during the fix.",
        repo_root=repo,
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "apps/web/app/admin/page.tsx" in auto, rows
    assert "apps/web/app/admin/overview/page.tsx" in auto, rows
    assert "/admin/page.tsx" not in auto, rows


def test_auto_extract_infers_sibling_filename_after_explicit_path(db_path):
    """A full path can establish directory context for a sibling filename."""
    task_id = _insert(
        str(db_path),
        "sibling shortform scope",
        (
            "Update tests/integration/test_create_task_scope.py and "
            "test_scope_cli.py together."
        ),
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_create_task_scope.py" in auto, rows
    assert "tests/integration/test_scope_cli.py" in auto, rows
    assert "test_scope_cli.py" not in auto, rows


def test_auto_extract_infers_comma_separated_paths_after_directory(db_path, tmp_path):
    """A nearby directory mention can scope a comma-separated filename list."""
    repo = tmp_path / "workflow-repo"
    repo.mkdir()
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@example.com"])
    _git(repo, ["config", "user.name", "Test User"])
    paths = [
        ".github/workflows/scraper-schedule.yml",
        ".github/workflows/scraper-verify.yml",
        ".github/workflows/podcast-episode-sync.yml",
    ]
    for path in paths:
        full_path = repo / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(f"# {path}\n", encoding="utf-8")
    _git(repo, ["add", *paths])
    _git(repo, ["commit", "-m", "seed workflows"])

    task_id = _insert(
        str(db_path),
        "workflow list scope",
        (
            "The change is duplicated across .github/workflows/: "
            "scraper-schedule.yml, scraper-verify.yml, podcast-episode-sync.yml."
        ),
        repo_root=repo,
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert ".github/workflows/scraper-schedule.yml" in auto, rows
    assert ".github/workflows/scraper-verify.yml" in auto, rows
    assert ".github/workflows/podcast-episode-sync.yml" in auto, rows


def test_auto_extract_resolves_unique_bare_filename_line_citation(db_path):
    """Bare ``filename:line`` citations resolve when the basename is unique."""
    task_id = _insert(
        str(db_path),
        "bare filename citation",
        "Update test_create_task_scope.py:350 to cover this regression.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_create_task_scope.py" in auto, rows
    assert "test_create_task_scope.py" not in auto, rows


def test_auto_extract_skips_ambiguous_bare_filename_line_citation(db_path):
    """Ambiguous bare basenames must not seed guessed task_scope rows."""
    task_id = _insert(
        str(db_path),
        "ambiguous bare filename citation",
        "Update conftest.py:50 after choosing the right test fixture.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "conftest.py" not in auto, rows
    assert not any(path.endswith("/conftest.py") for path in auto), rows


def test_auto_extract_infers_ios_test_target_shape(db_path):
    """Target-shaped iOS test names can seed the matching tracked path."""
    task_id = _insert(
        str(db_path),
        "ios target shape scope",
        (
            "Add a FooTests case under LaughTrackTests that drives the "
            "FooBar flow. Use the existing pattern documented in docs/example.md."
        ),
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/fixtures/ios/Tests/LaughTrackTests/FooTests.swift" in auto, rows
    assert "docs/example.md" not in auto, rows


def test_auto_extract_hydrates_numbered_file_set_from_task_commit(db_path, tmp_path):
    """A TASK commit reference plus numbered file-set prose hydrates git paths."""
    repo = tmp_path / "subject"
    repo.mkdir()
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@example.com"])
    _git(repo, ["config", "user.name", "Test User"])
    paths = [
        "venues/alpha/scraper.py",
        "venues/bravo/scraper.py",
        "venues/charlie/scraper.py",
        "venues/delta/scraper.py",
    ]
    for path in paths:
        full_path = repo / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(f"# {path}\n", encoding="utf-8")
    _git(repo, ["add", *paths])
    _git(repo, ["commit", "-m", "[TASK-123] seed venue scrapers"])
    sha = _git(repo, ["rev-parse", "--short=12", "HEAD"]).stdout.strip()

    task_id = _insert(
        str(db_path),
        "commit scope",
        (
            f"Update 4 venue scrapers from [TASK-X] commit {sha}. "
            "Particularly venues/alpha/scraper.py and venues/bravo/scraper.py."
        ),
        repo_root=repo,
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert set(paths).issubset(auto), rows
    assert sum(1 for r in rows if r["pattern"] == "venues/alpha/scraper.py") == 1
    assert sum(1 for r in rows if r["pattern"] == "venues/bravo/scraper.py") == 1


def test_auto_extract_ignores_missing_task_commit_sha(db_path, tmp_path):
    """An invalid referenced SHA should not block insertion or add fake paths."""
    repo = tmp_path / "subject"
    repo.mkdir()
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@example.com"])
    _git(repo, ["config", "user.name", "Test User"])
    existing = repo / "venues/alpha/scraper.py"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("# alpha\n", encoding="utf-8")
    _git(repo, ["add", "venues/alpha/scraper.py"])
    _git(repo, ["commit", "-m", "[TASK-1] seed one scraper"])

    task_id = _insert(
        str(db_path),
        "missing commit scope",
        (
            "Update 4 venue scrapers from [TASK-999] commit deadbee. "
            "Particularly venues/alpha/scraper.py."
        ),
        repo_root=repo,
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert auto == {"venues/alpha/scraper.py"}, rows


def test_auto_extract_does_not_nest_separate_explicit_path_mentions(db_path):
    """A later explicit path must not be treated as a sibling shortform."""
    task_id = _insert(
        str(db_path),
        "separate explicit path scope",
        (
            "Update skills/address-issue/SKILL.md and "
            "codex-prompts/address-issue.md together."
        ),
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "skills/address-issue/SKILL.md" in auto, rows
    assert "codex-prompts/address-issue.md" in auto, rows
    assert "skills/address-issue/address-issue.md" not in auto, rows


def test_auto_extract_infers_braced_sibling_filenames(db_path):
    """Brace shortforms inherit the nearby explicit path directory."""
    task_id = _insert(
        str(db_path),
        "braced sibling shortform scope",
        (
            "Update tests/integration/test_create_task_scope.py and "
            "{test_scope_cli,test_migrate_scope}.py together."
        ),
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_scope_cli.py" in auto, rows
    assert "tests/integration/test_migrate_scope.py" in auto, rows
    assert "{test_scope_cli,test_migrate_scope}.py" not in auto, rows


def test_auto_extract_infers_slash_separated_sibling_filenames(db_path):
    """Slash alternation applies to bare sibling filenames, not directories."""
    task_id = _insert(
        str(db_path),
        "slash sibling shortform scope",
        (
            "Update tests/integration/test_create_task_scope.py and "
            "test_scope_cli.py/test_migrate_scope.py together."
        ),
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "tests/integration/test_scope_cli.py" in auto, rows
    assert "tests/integration/test_migrate_scope.py" in auto, rows
    assert "test_scope_cli.py/test_migrate_scope.py" not in auto, rows


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


def test_auto_extract_rejects_dot_prefixed_prose_identifier_tokens(db_path):
    """SwiftUI modifier prose such as ``.sheet/.alert`` is not a path."""
    task_id = _insert(
        str(db_path),
        "swiftui modifiers",
        "This task description references the .sheet/.alert SwiftUI modifiers.",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert ".sheet/.alert" not in auto, rows
    assert ".sheet" not in auto, rows


def test_auto_extract_rejects_user_agent_version_tokens(db_path):
    """User-Agent strings such as ``Mozilla/5.0`` are not repo paths."""
    task_id = _insert(
        str(db_path),
        "probe sources",
        'Use curl -A "Mozilla/5.0" against each source before scraping.',
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "Mozilla/5.0" not in auto, rows
    assert "Mozilla" not in auto, rows


def test_auto_extract_rejects_runtime_dir_concatenation(db_path):
    """Runtime-dir prose such as ``node_modules/.venv`` is not a repo path.

    The dot-prefixed segment may appear in any position — the first-segment
    -only prose rule used to keep ``node_modules/.venv`` (issue #1093),
    landing a bogus auto_derived scope row that tripped a missing_scope_path
    context-health warning.
    """
    task_id = _insert(
        str(db_path),
        "worktree cleanup",
        "requires manual deletion of node_modules/.venv in the worktree",
    )

    rows = _scope_rows(str(db_path), task_id)
    auto = {r["pattern"] for r in rows if r["source"] == "auto_derived"}

    assert "node_modules/.venv" not in auto, rows
    assert ".venv/node_modules" not in auto, rows


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


def test_scope_hint_uses_auto_scope_path_heuristics(db_path, tmp_path):
    """scope-hint mirrors task-insert auto-derived path extraction."""
    repo = tmp_path / "scope-hint-repo"
    repo.mkdir()
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@example.com"])
    _git(repo, ["config", "user.name", "Test User"])
    paths = [
        "apps/web/app/admin/clubs/[id]/page.tsx",
        "apps/web/app/admin/page.tsx",
        ".github/workflows/scraper-schedule.yml",
        ".github/workflows/scraper-verify.yml",
    ]
    for path in paths:
        full_path = repo / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(f"# {path}\n", encoding="utf-8")
    _git(repo, ["add", *paths])
    _git(repo, ["commit", "-m", "seed scoped files"])

    result = _run([
        "scope-hint",
        "--description",
        (
            "Touch apps/web/app/admin/clubs/[id]/page.tsx and /admin/page.tsx. "
            "Duplicated across .github/workflows/: scraper-schedule.yml, "
            "scraper-verify.yml."
        ),
        "--task-type",
        "bug",
        "--repo-root",
        str(repo),
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert "apps/web/app/admin/clubs/[id]/page.tsx" in payload["scope"], payload
    assert "apps/web/app/admin/page.tsx" in payload["scope"], payload
    assert ".github/workflows/scraper-schedule.yml" in payload["scope"], payload
    assert ".github/workflows/scraper-verify.yml" in payload["scope"], payload


def test_scope_hint_extracts_bare_github_subdirectories(db_path, tmp_path):
    """Bare .github/<subdir>/ mentions should produce scoped directory rows."""
    repo = tmp_path / "scope-hint-github-dir-repo"
    repo.mkdir()
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "test@example.com"])
    _git(repo, ["config", "user.name", "Test User"])
    paths = [
        ".github/workflows/web-ci.yml",
        ".github/actions/setup/action.yml",
    ]
    for path in paths:
        full_path = repo / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(f"# {path}\n", encoding="utf-8")
    _git(repo, ["add", *paths])
    _git(repo, ["commit", "-m", "seed github scoped files"])

    result = _run([
        "scope-hint",
        "--description",
        "Sweep .github/workflows/ and .github/actions/ for Node 24 support.",
        "--task-type",
        "issue",
        "--repo-root",
        str(repo),
    ])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert ".github/workflows/**" in payload["scope"], payload
    assert ".github/actions/**" in payload["scope"], payload


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
