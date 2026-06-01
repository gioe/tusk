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
