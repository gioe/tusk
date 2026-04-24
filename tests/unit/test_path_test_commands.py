"""Unit tests for path-scoped test command resolution.

Covers:
- ``match_path_test_command`` / ``_match_path_test_command`` pure matching:
  single glob match, multi-pattern fallback, catch-all ``*``, no match.
- ``load_test_command`` priority: path_test_commands → domain_test_commands
  → global test_command.
- ``resolve_test_command`` (precheck) path-aware priority and fallback when
  path_test_commands is absent.
- Ambiguous-match semantics: when staged paths span multiple patterns, no
  single-pattern match wins and resolution falls through (deterministic).
"""

import importlib.util
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


commit_mod = _load("tusk_commit", "tusk-commit.py")
precheck_mod = _load("tusk_test_precheck", "tusk-test-precheck.py")


def _write_config(tmp_path, data: dict) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return str(p)


# ---------------------------------------------------------------------------
# Pure matching
# ---------------------------------------------------------------------------


class TestMatchPathTestCommand:
    def test_single_pattern_covers_all_paths(self):
        patterns = {"apps/scraper/*": "pytest scraper"}
        assert commit_mod.match_path_test_command(patterns, ["apps/scraper/foo.py"]) == "pytest scraper"
        assert commit_mod.match_path_test_command(patterns, ["apps/scraper/pkg/bar.py"]) == "pytest scraper"

    def test_star_matches_across_path_separators(self):
        """fnmatch ``*`` already matches across ``/`` — users can write ``apps/scraper/**`` or ``apps/scraper/*``."""
        patterns = {"apps/scraper/**": "pytest scraper"}
        assert commit_mod.match_path_test_command(patterns, ["apps/scraper/deep/nested/file.py"]) == "pytest scraper"

    def test_mixed_paths_without_catchall_returns_empty(self):
        """When staged paths span multiple patterns and no single pattern covers them all, resolution falls through."""
        patterns = {
            "apps/scraper/*": "pytest scraper",
            "ios/*": "xcodebuild test",
        }
        assert commit_mod.match_path_test_command(
            patterns, ["apps/scraper/foo.py", "ios/bar.swift"]
        ) == ""

    def test_catchall_wins_when_no_specific_match(self):
        """Trailing ``*`` serves as the explicit catch-all fallback."""
        patterns = {
            "apps/scraper/*": "pytest scraper",
            "*": "pytest all",
        }
        assert commit_mod.match_path_test_command(
            patterns, ["apps/scraper/foo.py", "ios/bar.swift"]
        ) == "pytest all"

    def test_insertion_order_preserved(self):
        """First pattern in insertion order wins when multiple match."""
        patterns = {
            "apps/scraper/*": "pytest scraper",
            "*": "pytest all",
        }
        assert commit_mod.match_path_test_command(patterns, ["apps/scraper/foo.py"]) == "pytest scraper"

    def test_empty_patterns_returns_empty(self):
        assert commit_mod.match_path_test_command({}, ["apps/scraper/foo.py"]) == ""

    def test_empty_paths_returns_empty(self):
        assert commit_mod.match_path_test_command({"*": "pytest all"}, []) == ""

    def test_empty_command_string_skipped(self):
        """An explicit empty string disables that pattern instead of silencing tests."""
        patterns = {
            "apps/scraper/*": "",
            "*": "pytest all",
        }
        assert commit_mod.match_path_test_command(patterns, ["apps/scraper/foo.py"]) == "pytest all"


# ---------------------------------------------------------------------------
# load_test_command priority (commit path)
# ---------------------------------------------------------------------------


class TestLoadTestCommandPriority:
    def test_path_beats_domain_and_global(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"cli": "pytest tests/cli/"},
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        assert commit_mod.load_test_command(
            config, domain="cli", paths=["apps/scraper/foo.py"]
        ) == "pytest scraper"

    def test_domain_used_when_no_path_match(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"cli": "pytest tests/cli/"},
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        assert commit_mod.load_test_command(
            config, domain="cli", paths=["docs/README.md"]
        ) == "pytest tests/cli/"

    def test_global_used_when_path_and_domain_miss(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"cli": "pytest tests/cli/"},
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        assert commit_mod.load_test_command(
            config, domain="install", paths=["docs/README.md"]
        ) == "pytest tests/"

    def test_no_path_test_commands_key_falls_back_to_domain(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"cli": "pytest tests/cli/"},
        })
        assert commit_mod.load_test_command(
            config, domain="cli", paths=["apps/scraper/foo.py"]
        ) == "pytest tests/cli/"

    def test_no_paths_arg_preserves_legacy_behavior(self, tmp_path):
        """Callers that don't pass paths keep the old domain > global semantics."""
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "domain_test_commands": {"cli": "pytest tests/cli/"},
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        assert commit_mod.load_test_command(config, domain="cli") == "pytest tests/cli/"

    def test_ambiguous_match_falls_through_to_domain(self, tmp_path):
        """Staged files spanning multiple path patterns with no catch-all fall through."""
        config = _write_config(tmp_path, {
            "test_command": "pytest all",
            "domain_test_commands": {"cli": "pytest cli-tests"},
            "path_test_commands": {
                "apps/scraper/*": "pytest scraper",
                "ios/*": "xcodebuild test",
            },
        })
        assert commit_mod.load_test_command(
            config, domain="cli", paths=["apps/scraper/foo.py", "ios/bar.swift"]
        ) == "pytest cli-tests"


# ---------------------------------------------------------------------------
# precheck resolve_test_command path priority
# ---------------------------------------------------------------------------


class TestPrecheckResolveTestCommand:
    def test_explicit_command_wins_over_path(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        cmd = precheck_mod.resolve_test_command(
            explicit="pytest --lf",
            config_path=config,
            repo_root=str(tmp_path),
            script_dir=BIN,
            paths=["apps/scraper/foo.py"],
        )
        assert cmd == "pytest --lf"

    def test_path_beats_global_when_paths_provided(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        cmd = precheck_mod.resolve_test_command(
            explicit="",
            config_path=config,
            repo_root=str(tmp_path),
            script_dir=BIN,
            paths=["apps/scraper/foo.py"],
        )
        assert cmd == "pytest scraper"

    def test_ambiguous_paths_fall_back_to_global(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "path_test_commands": {
                "apps/scraper/*": "pytest scraper",
                "ios/*": "xcodebuild test",
            },
        })
        cmd = precheck_mod.resolve_test_command(
            explicit="",
            config_path=config,
            repo_root=str(tmp_path),
            script_dir=BIN,
            paths=["apps/scraper/foo.py", "ios/bar.swift"],
        )
        assert cmd == "pytest tests/"

    def test_no_path_test_commands_skips_path_matching(self, tmp_path):
        config = _write_config(tmp_path, {"test_command": "pytest tests/"})
        cmd = precheck_mod.resolve_test_command(
            explicit="",
            config_path=config,
            repo_root=str(tmp_path),
            script_dir=BIN,
            paths=["apps/scraper/foo.py"],
        )
        assert cmd == "pytest tests/"
