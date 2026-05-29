"""Unit tests for the non-code-only test-gate skip in tusk-commit.py (issue #950).

`tusk commit` skips the test_command gate when every staged file is non-code —
a docs/markdown file (*.md) or a scope.always_allowed metadata file (VERSION,
CHANGELOG.md, MANIFEST, .claude/tusk-manifest.json) — since such commits cannot
change test outcomes. These tests exercise the two pure helpers that make that
decision: `_resolve_non_code_allowlist` and `_all_staged_files_non_code`.
"""

import importlib.util
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_commit", COMMIT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_config(tmp_path, data: dict) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


class TestResolveNonCodeAllowlist:
    def test_project_always_allowed_wins(self, tmp_path):
        mod = _load_module()
        config_path = _write_config(
            tmp_path, {"scope": {"always_allowed": ["VERSION", "docs/NOTES"]}}
        )
        assert mod._resolve_non_code_allowlist(config_path) == {"VERSION", "docs/NOTES"}

    def test_missing_scope_falls_back_to_defaults(self, tmp_path):
        mod = _load_module()
        config_path = _write_config(tmp_path, {"test_command": "pytest"})
        assert mod._resolve_non_code_allowlist(config_path) == set(
            mod._DEFAULT_NON_CODE_FILES
        )

    def test_empty_always_allowed_falls_back_to_defaults(self, tmp_path):
        mod = _load_module()
        config_path = _write_config(tmp_path, {"scope": {"always_allowed": []}})
        assert mod._resolve_non_code_allowlist(config_path) == set(
            mod._DEFAULT_NON_CODE_FILES
        )

    def test_missing_config_file_falls_back_to_defaults(self, tmp_path):
        mod = _load_module()
        missing = str(tmp_path / "does-not-exist.json")
        assert mod._resolve_non_code_allowlist(missing) == set(
            mod._DEFAULT_NON_CODE_FILES
        )

    def test_malformed_config_falls_back_to_defaults(self, tmp_path):
        mod = _load_module()
        p = tmp_path / "config.json"
        p.write_text("{not json", encoding="utf-8")
        assert mod._resolve_non_code_allowlist(str(p)) == set(
            mod._DEFAULT_NON_CODE_FILES
        )

    def test_defaults_include_canonical_metadata_files(self):
        mod = _load_module()
        assert set(mod._DEFAULT_NON_CODE_FILES) == {
            "VERSION",
            "CHANGELOG.md",
            "MANIFEST",
            ".claude/tusk-manifest.json",
        }


class TestAllStagedFilesNonCode:
    def setup_method(self):
        self.mod = _load_module()
        self.allowlist = set(self.mod._DEFAULT_NON_CODE_FILES)

    def test_version_and_changelog_only_is_non_code(self):
        assert self.mod._all_staged_files_non_code(
            ["VERSION", "CHANGELOG.md"], self.allowlist
        )

    def test_markdown_anywhere_is_non_code(self):
        assert self.mod._all_staged_files_non_code(
            ["docs/DOMAIN.md", "skills/tusk/SKILL.md", "README.md"], self.allowlist
        )

    def test_manifest_files_are_non_code(self):
        assert self.mod._all_staged_files_non_code(
            ["MANIFEST", ".claude/tusk-manifest.json"], self.allowlist
        )

    def test_any_code_file_makes_it_code(self):
        assert not self.mod._all_staged_files_non_code(
            ["VERSION", "bin/tusk-commit.py"], self.allowlist
        )

    def test_single_code_file_is_code(self):
        assert not self.mod._all_staged_files_non_code(
            ["tests/unit/test_foo.py"], self.allowlist
        )

    def test_empty_input_returns_false(self):
        # Nothing to reason about → caller's normal (gate-runs) path is the safe default.
        assert not self.mod._all_staged_files_non_code([], self.allowlist)

    def test_markdown_match_is_case_insensitive(self):
        assert self.mod._all_staged_files_non_code(["docs/FOO.MD"], self.allowlist)

    def test_non_md_non_allowlisted_doc_is_code(self):
        # A .txt or .rst file is not covered by the *.md glob or the allowlist,
        # so it is treated as code (conservative — the gate runs).
        assert not self.mod._all_staged_files_non_code(["docs/NOTES.txt"], self.allowlist)

    def test_project_allowlist_entry_recognized(self):
        # A project that lists docs/NOTES.txt in scope.always_allowed gets it
        # recognized as non-code even though it is not *.md.
        allowlist = {"docs/NOTES.txt"}
        assert self.mod._all_staged_files_non_code(["docs/NOTES.txt"], allowlist)
        assert not self.mod._all_staged_files_non_code(["VERSION"], allowlist)
