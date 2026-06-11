"""Unit coverage for lockfile sibling scope derivation (issue #1052).

When auto-derivation produces a scope row whose basename is package.json and
the same text block names a lockfile, the sibling lockfile row for that
directory is emitted too — a package.json edit and its lockfile regeneration
always travel together.
"""

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


def test_issue_repro_derives_lockfile_sibling():
    text = (
        "Remove a dep from package.json (apps/web/package.json line 37), "
        "regenerate package-lock.json via npm install."
    )
    candidates = mod._auto_scope_candidates(text)
    assert "apps/web/package.json" in candidates
    assert "apps/web/package-lock.json" in candidates


def test_no_lockfile_mention_emits_no_sibling():
    text = "Edit apps/web/package.json to bump react"
    candidates = mod._auto_scope_candidates(text)
    assert "apps/web/package.json" in candidates
    assert not any(c.endswith("package-lock.json") for c in candidates)
    assert not any(c.endswith("yarn.lock") for c in candidates)
    assert not any(c.endswith("pnpm-lock.yaml") for c in candidates)


def test_yarn_and_pnpm_lockfiles_pair_too():
    yarn = mod._lockfile_sibling_scope_paths(
        "Update apps/api/package.json and refresh yarn.lock",
        ["apps/api/package.json"],
    )
    assert yarn == ["apps/api/yarn.lock"]

    pnpm = mod._lockfile_sibling_scope_paths(
        "Update package.json and regenerate pnpm-lock.yaml",
        ["package.json"],
    )
    assert pnpm == ["pnpm-lock.yaml"]


def test_no_package_json_candidate_means_no_sibling():
    siblings = mod._lockfile_sibling_scope_paths(
        "regenerate package-lock.json via npm install",
        ["apps/web/src/index.ts"],
    )
    assert siblings == []
