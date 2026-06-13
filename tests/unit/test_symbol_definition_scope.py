"""Unit tests for symbol-to-definition scope derivation (issue #1080).

Tasks routinely describe scope via code symbols ("extend
LINEUP_COMEDIAN_SELECT and mapLineupItem ...") without naming the defining
files; the path extractor cannot see those, which forced 6
expanded_mid_task rows in the original incident. ``_auto_scope_candidates``
now resolves likely symbol tokens (SCREAMING_SNAKE constants, camelCase
identifiers) to their unique definition files via git grep — multi-file and
zero-match symbols are skipped silently.
"""

import importlib.util
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
INSERT_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-insert.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_task_insert", INSERT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def symbol_repo(tmp_path):
    """A repo mirroring the issue #1080 incident shape."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "t@example.test"], cwd=repo)
    _git(["config", "user.name", "T"], cwd=repo)
    files = {
        "lib/data/comedian/lineupComedianSelect.ts":
            "export const LINEUP_COMEDIAN_SELECT = { id: true };\n",
        "util/comedian/comedianUtil.ts":
            "export function mapLineupItem(x) { return x; }\n",
        "lib/data/show/findShowById.ts":
            "export function findShowById(id) { return id; }\n",
        # An ambiguous symbol defined in TWO files:
        "lib/a.ts": "export const SHARED_AMBIGUOUS_CONST = 1;\n",
        "lib/b.ts": "export const SHARED_AMBIGUOUS_CONST = 2;\n",
        # A python-style definition:
        "scraper/parse.py": "def parseVenueLineup(html):\n    return html\n",
        "README.md": "docs\n",
    }
    for rel, content in files.items():
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    return repo


class TestExtractSymbolTokens:
    def test_extracts_screaming_snake_and_camel_case(self, mod):
        text = "Extend LINEUP_COMEDIAN_SELECT and mapLineupItem so items expose popularity"
        tokens = mod._extract_symbol_tokens(text)
        assert "LINEUP_COMEDIAN_SELECT" in tokens
        assert "mapLineupItem" in tokens

    def test_skips_prose_cased_and_short_tokens(self, mod):
        text = "GitHub and JavaScript on macOS use JSON via TASK-123"
        assert mod._extract_symbol_tokens(text) == []

    def test_caps_token_count(self, mod):
        text = " ".join(f"SYMBOL_NUMBER_{i} myCamelToken{i}" for i in range(20))
        assert len(mod._extract_symbol_tokens(text)) == mod._SYMBOL_SCAN_CAP


class TestSymbolDefinitionScopePaths:
    def test_resolves_unique_definitions(self, mod, symbol_repo):
        text = (
            "Extend LINEUP_COMEDIAN_SELECT and mapLineupItem via findShowById "
            "so lineup items expose popularity"
        )
        paths = mod._symbol_definition_scope_paths(str(symbol_repo), text, [])
        assert "lib/data/comedian/lineupComedianSelect.ts" in paths
        assert "util/comedian/comedianUtil.ts" in paths
        assert "lib/data/show/findShowById.ts" in paths

    def test_python_def_shape_resolves(self, mod, symbol_repo):
        paths = mod._symbol_definition_scope_paths(
            str(symbol_repo), "Harden parseVenueLineup against empty pages", []
        )
        assert paths == ["scraper/parse.py"]

    def test_ambiguous_symbol_is_skipped(self, mod, symbol_repo):
        paths = mod._symbol_definition_scope_paths(
            str(symbol_repo), "Adjust SHARED_AMBIGUOUS_CONST handling", []
        )
        assert paths == []

    def test_unknown_symbol_is_skipped(self, mod, symbol_repo):
        paths = mod._symbol_definition_scope_paths(
            str(symbol_repo), "Wire NONEXISTENT_FANCY_CONST into the flow", []
        )
        assert paths == []

    def test_known_paths_are_not_duplicated(self, mod, symbol_repo):
        paths = mod._symbol_definition_scope_paths(
            str(symbol_repo),
            "Extend mapLineupItem in util/comedian/comedianUtil.ts",
            ["util/comedian/comedianUtil.ts"],
        )
        assert paths == []

    def test_no_repo_root_is_silent(self, mod):
        assert mod._symbol_definition_scope_paths(None, "mapLineupItem", []) == []

    def test_outside_git_repo_is_silent(self, mod, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert (
            mod._symbol_definition_scope_paths(str(plain), "mapLineupItem etc", [])
            == []
        )


class TestAutoScopeCandidatesIntegration:
    def test_incident_shape_yields_symbol_definitions(self, mod, symbol_repo):
        """The issue #1080 description shape: explicit path + bare symbols.
        Candidates must include the symbol definition files alongside the
        explicit path."""
        text = (
            "findShowById via LINEUP_COMEDIAN_SELECT / mapLineupItem in "
            "util/comedian/comedianUtil.ts"
        )
        candidates = mod._auto_scope_candidates(text, repo_root=str(symbol_repo))
        assert "util/comedian/comedianUtil.ts" in candidates
        assert "lib/data/comedian/lineupComedianSelect.ts" in candidates
        assert "lib/data/show/findShowById.ts" in candidates
