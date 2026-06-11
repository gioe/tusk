"""Unit coverage for sibling test file scope derivation (issue #1073).

When a criterion or description explicitly requires unit tests and
auto-derivation produces a source-file scope row, the conventional sibling
test file that exists on disk is emitted too — the unit-test requirement
means the test edit travels with the source edit, and deriving one without
the other forces a mid-task scope expansion.
"""

import importlib.util
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_task_insert",
    os.path.join(BIN, "tusk-task-insert.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture
def fake_repo(tmp_path):
    src = tmp_path / "apps" / "web" / "ui" / "ticketCta"
    src.mkdir(parents=True)
    (src / "index.tsx").write_text("export {}\n", encoding="utf-8")
    (src / "index.test.tsx").write_text("test\n", encoding="utf-8")
    py_dir = tmp_path / "lib"
    py_dir.mkdir()
    (py_dir / "util.py").write_text("x = 1\n", encoding="utf-8")
    (py_dir / "test_util.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return str(tmp_path)


class TestTestSiblingScopePaths:
    def test_js_sibling_derived_when_on_disk(self, fake_repo):
        siblings = mod._test_sibling_scope_paths(
            fake_repo, ["apps/web/ui/ticketCta/index.tsx"]
        )
        assert siblings == ["apps/web/ui/ticketCta/index.test.tsx"]

    def test_python_sibling_derived_when_on_disk(self, fake_repo):
        siblings = mod._test_sibling_scope_paths(fake_repo, ["lib/util.py"])
        assert siblings == ["lib/test_util.py"]

    def test_nonexistent_sibling_not_derived(self, fake_repo):
        os.remove(os.path.join(fake_repo, "apps/web/ui/ticketCta/index.test.tsx"))
        siblings = mod._test_sibling_scope_paths(
            fake_repo, ["apps/web/ui/ticketCta/index.tsx"]
        )
        assert siblings == []

    def test_test_file_candidate_does_not_rederive(self, fake_repo):
        # A candidate that already is a test file must not chain (index.test
        # -> index.test.test) nor re-emit itself.
        siblings = mod._test_sibling_scope_paths(
            fake_repo, ["apps/web/ui/ticketCta/index.test.tsx", "lib/test_util.py"]
        )
        assert siblings == []

    def test_no_repo_root_derives_nothing(self):
        assert mod._test_sibling_scope_paths(None, ["a/b.ts"]) == []


class TestAutoScopeCandidatesGate:
    def test_issue_repro_derives_sibling_with_unit_test_criterion(self, fake_repo):
        # Trigger and path in separate blocks, joined via requires_unit_tests
        # — mirrors task-insert/task-update/scope-hint iterating blocks.
        candidates = mod._auto_scope_candidates(
            "Fix apps/web/ui/ticketCta/index.tsx rendering",
            repo_root=fake_repo,
            requires_unit_tests=True,
        )
        assert "apps/web/ui/ticketCta/index.tsx" in candidates
        assert "apps/web/ui/ticketCta/index.test.tsx" in candidates

    def test_no_unit_test_requirement_means_no_sibling(self, fake_repo):
        candidates = mod._auto_scope_candidates(
            "Fix apps/web/ui/ticketCta/index.tsx rendering",
            repo_root=fake_repo,
            requires_unit_tests=False,
        )
        assert "apps/web/ui/ticketCta/index.tsx" in candidates
        assert "apps/web/ui/ticketCta/index.test.tsx" not in candidates

    def test_same_block_mention_detected_by_default(self, fake_repo):
        # requires_unit_tests=None (default) detects from the block itself.
        candidates = mod._auto_scope_candidates(
            "Fix apps/web/ui/ticketCta/index.tsx; unit tests cover the case",
            repo_root=fake_repo,
        )
        assert "apps/web/ui/ticketCta/index.test.tsx" in candidates


class TestUnitTestRequirementRe:
    @pytest.mark.parametrize(
        "text",
        [
            "Unit tests cover the duplicate-room case and pass",
            "add a unit-test for the helper",
            "unit_tests must pass",
        ],
    )
    def test_matches_explicit_unit_test_phrases(self, text):
        assert mod._UNIT_TEST_REQUIREMENT_RE.search(text)

    @pytest.mark.parametrize(
        "text",
        [
            "all tests pass",
            "integration tests cover the flow",
            "test the deploy manually",
        ],
    )
    def test_does_not_match_generic_test_prose(self, text):
        assert not mod._UNIT_TEST_REQUIREMENT_RE.search(text)
