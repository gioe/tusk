"""Unit coverage for task-insert auto-derived scope path resolution."""

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_insert",
    os.path.join(BIN, "tusk-task-insert.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_scope_hint_spec = importlib.util.spec_from_file_location(
    "tusk_scope_hint",
    os.path.join(BIN, "tusk-scope-hint.py"),
)
scope_hint = importlib.util.module_from_spec(_scope_hint_spec)
_scope_hint_spec.loader.exec_module(scope_hint)


def test_resolve_auto_scope_keeps_ambiguous_suffix_literal(monkeypatch):
    monkeypatch.setattr(mod, "path_exists_in_repo", lambda repo, path: False)
    monkeypatch.setattr(
        mod,
        "_tracked_repo_files",
        lambda repo: [
            "apps/web/tests/shared/test_scope.py",
            "apps/api/tests/shared/test_scope.py",
        ],
    )

    assert (
        mod._resolve_auto_derived_scope_pattern("repo", "tests/shared/test_scope.py")
        == "tests/shared/test_scope.py"
    )


def test_resolve_auto_scope_returns_unique_suffix_match(monkeypatch):
    monkeypatch.setattr(mod, "path_exists_in_repo", lambda repo, path: False)
    monkeypatch.setattr(
        mod,
        "_tracked_repo_files",
        lambda repo: [
            "apps/web/tests/shared/test_scope.py",
            "apps/api/tests/other/test_scope.py",
        ],
    )

    assert (
        mod._resolve_auto_derived_scope_pattern("repo", "tests/shared/test_scope.py")
        == "apps/web/tests/shared/test_scope.py"
    )


def test_resolve_auto_scope_strips_nodeid_when_file_exists(monkeypatch):
    monkeypatch.setattr(
        mod,
        "path_exists_in_repo",
        lambda repo, path: path == "tests/integration/test_create_task_scope.py",
    )
    monkeypatch.setattr(mod, "_tracked_repo_files", lambda repo: [])

    assert (
        mod._resolve_auto_derived_scope_pattern(
            "repo",
            "tests/integration/test_create_task_scope.py::test_case",
        )
        == "tests/integration/test_create_task_scope.py"
    )


def test_auto_scope_candidates_keep_bracketed_route_segments():
    text = "\n".join(
        [
            "- apps/web/app/api/v1/favorites/route.test.ts:262-276",
            "- apps/web/app/api/v1/comedians/[id]/route.test.ts:292-306",
            "- apps/web/util/comedian/comedianUtil.test.ts:224-231",
        ]
    )

    assert mod._auto_scope_candidates(text, repo_root="repo", task_type="refactor")[:3] == [
        "apps/web/app/api/v1/favorites/route.test.ts",
        "apps/web/app/api/v1/comedians/[id]/route.test.ts",
        "apps/web/util/comedian/comedianUtil.test.ts",
    ]


def test_auto_scope_candidates_expand_brace_list_paths_with_item_extensions():
    text = (
        "Files: bin/{tusk-task-insert.py,tusk-task-update.py}, "
        "docs/DOMAIN.md"
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert candidates[:3] == [
        "docs/DOMAIN.md",
        "bin/tusk-task-insert.py",
        "bin/tusk-task-update.py",
    ]


def test_auto_scope_candidates_keep_explicit_single_stack_paths_over_target_noise(monkeypatch):
    text = (
        "Files: bin/{tusk-task-insert.py,tusk-task-update.py}, docs/DOMAIN.md. "
        "The unrelated FooTests target should not displace explicit scope."
    )
    monkeypatch.setattr(
        mod,
        "_tracked_repo_files",
        lambda repo: [
            "tests/fixtures/ios/Tests/LaughTrackTests/FooTests.swift",
            "bin/tusk-task-insert.py",
            "bin/tusk-task-update.py",
            "docs/DOMAIN.md",
        ],
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert candidates[:3] == [
        "docs/DOMAIN.md",
        "bin/tusk-task-insert.py",
        "bin/tusk-task-update.py",
    ]
    assert "tests/fixtures/ios/Tests/LaughTrackTests/FooTests.swift" not in candidates


def test_auto_scope_candidates_drop_target_noise_from_different_app_stack(monkeypatch):
    text = (
        "Files: apps/scraper/scrapers/foo/{scraper.py,extractor.py}. "
        "The weak ComedianDetailViewTests token belongs to a different app."
    )
    monkeypatch.setattr(
        mod,
        "_tracked_repo_files",
        lambda repo: [
            "apps/web/ios/Tests/ComedianDetailViewTests.swift",
            "apps/scraper/scrapers/foo/scraper.py",
            "apps/scraper/scrapers/foo/extractor.py",
        ],
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert candidates[:2] == [
        "apps/scraper/scrapers/foo/scraper.py",
        "apps/scraper/scrapers/foo/extractor.py",
    ]
    assert "apps/web/ios/Tests/ComedianDetailViewTests.swift" not in candidates


def test_auto_scope_candidates_include_git_rm_directory_operand():
    text = (
        "git rm -r --cached .claude/bin and commit; "
        "diff each modified .claude/skills/*/SKILL.md. "
        "Files stay on disk after rm --cached; AGENTS.md runtime invocation is unaffected."
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert ".claude/bin/**" in candidates


def test_auto_scope_candidates_preserve_skill_glob_without_bare_basename():
    text = (
        "git rm -r --cached .claude/bin and commit; "
        "diff each modified .claude/skills/*/SKILL.md. "
        "Files stay on disk after rm --cached; AGENTS.md runtime invocation is unaffected."
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert ".claude/bin/**" in candidates
    assert ".claude/skills/*/SKILL.md" in candidates
    assert "SKILL.md" not in candidates


def test_scope_hint_shares_git_command_operand_derivation(monkeypatch):
    text = "git rm -r --cached .claude/bin and commit"
    monkeypatch.setattr(
        scope_hint._git_helpers,
        "is_trackable_scope_pattern",
        lambda _root, _pattern, **_kwargs: True,
    )

    candidates = scope_hint._extract_scope([text], repo_root="repo", task_type="bug")

    assert ".claude/bin/**" in candidates


def test_scope_hint_drops_foreign_missing_path_under_existing_top_level(monkeypatch):
    monkeypatch.setattr(
        scope_hint._git_helpers,
        "is_trackable_scope_pattern",
        lambda _root, _pattern, *, allow_new_under_tracked: allow_new_under_tracked,
    )

    candidates = scope_hint._extract_scope(
        ["The reporter's project uses .github/workflows/ios.yml."],
        repo_root="repo",
        task_type="bug",
    )

    assert ".github/workflows/ios.yml" not in candidates


def test_scope_hint_keeps_explicitly_created_new_path(monkeypatch):
    monkeypatch.setattr(
        scope_hint._git_helpers,
        "is_trackable_scope_pattern",
        lambda _root, _pattern, *, allow_new_under_tracked: allow_new_under_tracked,
    )
    text = "Create a new test tests/unit/test_new_guard.py."

    candidates = scope_hint._extract_scope([text], repo_root="repo", task_type="bug")

    assert "tests/unit/test_new_guard.py" in candidates
    assert scope_hint._extract_creates([text]) == ["tests/unit/test_new_guard.py"]


def test_auto_scope_candidates_drop_negated_git_command_operand():
    text = "Do not run git rm -r --cached .claude/bin; only inspect the current state."

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert ".claude/bin/**" not in candidates


def test_auto_scope_candidates_drop_unaffected_path_mention():
    text = (
        "git rm -r --cached .claude/bin and commit; "
        "AGENTS.md runtime invocation is unaffected."
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert ".claude/bin/**" in candidates
    assert "AGENTS.md" not in candidates


def test_auto_scope_candidates_include_git_mv_and_cp_operands():
    text = (
        "Run git mv docs/old-guide.md docs/new-guide.md, then "
        "cp scripts/template.sh scripts/generated.sh."
    )

    candidates = mod._auto_scope_candidates(text, repo_root="repo", task_type="bug")

    assert "docs/old-guide.md" in candidates
    assert "docs/new-guide.md" in candidates
    assert "scripts/template.sh" in candidates
    assert "scripts/generated.sh" in candidates
