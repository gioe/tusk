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
config_tools_mod = _load("tusk_config_tools", "tusk-config-tools.py")


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


class TestAbsolutePathNormalization:
    """Regression: absolute path inputs must still match repo-relative patterns.

    ``tusk-commit.py`` stores absolute paths unchanged in ``resolved_files``
    when the user passes absolute file paths to ``tusk commit`` (see the
    ``isabs`` branch in ``_run_commit``). Without normalization fnmatch would
    not match ``apps/scraper/*`` against
    ``/Users/foo/repo/apps/scraper/bar.py`` and path_test_commands would
    silently fall through.
    """

    def test_commit_matcher_normalizes_absolute_paths(self):
        patterns = {"apps/scraper/*": "pytest scraper"}
        repo_root = "/Users/foo/repo"
        abs_path = "/Users/foo/repo/apps/scraper/bar.py"
        assert commit_mod.match_path_test_command(patterns, [abs_path], repo_root) == "pytest scraper"

    def test_commit_matcher_handles_mixed_abs_and_rel_paths(self):
        patterns = {"apps/scraper/*": "pytest scraper"}
        repo_root = "/Users/foo/repo"
        paths = ["/Users/foo/repo/apps/scraper/a.py", "apps/scraper/b.py"]
        assert commit_mod.match_path_test_command(patterns, paths, repo_root) == "pytest scraper"

    def test_load_test_command_threads_repo_root(self, tmp_path):
        config = _write_config(tmp_path, {
            "test_command": "pytest tests/",
            "path_test_commands": {"apps/scraper/*": "pytest scraper"},
        })
        abs_path = f"{tmp_path}/apps/scraper/foo.py"
        cmd = commit_mod.load_test_command(
            config, domain="", paths=[abs_path], repo_root=str(tmp_path),
        )
        assert cmd == "pytest scraper"

    def test_precheck_matcher_normalizes_absolute_paths(self):
        patterns = {"apps/scraper/*": "pytest scraper"}
        repo_root = "/Users/foo/repo"
        abs_path = "/Users/foo/repo/apps/scraper/bar.py"
        assert precheck_mod._match_path_test_command(patterns, [abs_path], repo_root) == "pytest scraper"


class TestConfigValidatorPathTestCommands:
    """Validator coverage for the path_test_commands shape.

    Mirrors the validator logic in ``bin/tusk-config-tools.py``. Exercises
    the ``cmd_validate`` entry point so a future refactor that drops one of
    the inline checks fails loudly.
    """

    def _run_validate(self, tmp_path, cfg: dict) -> int:
        p = tmp_path / "config.json"
        p.write_text(json.dumps({
            "statuses": ["To Do", "Done"],
            "priorities": ["High", "Medium", "Low"],
            "closed_reasons": ["completed", "expired"],
            **cfg,
        }))
        return config_tools_mod.cmd_validate(str(p))

    def test_rejects_non_object(self, tmp_path, capsys):
        rc = self._run_validate(tmp_path, {"path_test_commands": ["not", "an", "object"]})
        err = capsys.readouterr().err
        assert rc == 1
        assert 'path_test_commands" must be an object' in err

    def test_rejects_string(self, tmp_path, capsys):
        rc = self._run_validate(tmp_path, {"path_test_commands": "not an object"})
        err = capsys.readouterr().err
        assert rc == 1
        assert 'path_test_commands" must be an object' in err

    def test_rejects_empty_string_key(self, tmp_path, capsys):
        rc = self._run_validate(tmp_path, {"path_test_commands": {"": "pytest all"}})
        err = capsys.readouterr().err
        assert rc == 1
        assert "keys must be non-empty strings" in err

    def test_rejects_non_string_value(self, tmp_path, capsys):
        rc = self._run_validate(tmp_path, {"path_test_commands": {"*": 42}})
        err = capsys.readouterr().err
        assert rc == 1
        assert 'path_test_commands.*" value must be a string' in err

    def test_accepts_valid_shape(self, tmp_path):
        rc = self._run_validate(tmp_path, {
            "path_test_commands": {
                "apps/scraper/*": "pytest scraper",
                "*": "pytest all",
            },
        })
        assert rc == 0

    def test_accepts_empty_object(self, tmp_path):
        rc = self._run_validate(tmp_path, {"path_test_commands": {}})
        assert rc == 0


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
