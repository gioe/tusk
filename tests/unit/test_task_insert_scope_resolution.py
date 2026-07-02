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
