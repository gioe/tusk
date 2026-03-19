"""Unit tests for rule7_config_keys_match_known_keys in tusk-lint.py.

Covers the happy path (all config keys present in KNOWN_KEYS) and the
violation path (a config key absent from KNOWN_KEYS is flagged).
"""

import importlib.util
import json
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _make_root(config_keys: list[str], known_keys: list[str]) -> tempfile.TemporaryDirectory:
    """Return a TemporaryDirectory containing a minimal mock project."""
    tmp = tempfile.TemporaryDirectory()
    root = tmp.name

    # Write config.default.json with the given top-level keys (empty-array values)
    cfg = {k: [] for k in config_keys}
    with open(os.path.join(root, "config.default.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    # Write bin/tusk-config-tools.py with a KNOWN_KEYS set literal
    bin_dir = os.path.join(root, "bin")
    os.makedirs(bin_dir)
    keys_literal = ", ".join(f"'{k}'" for k in known_keys)
    tools_content = f"KNOWN_KEYS = {{{keys_literal}}}\n"
    with open(os.path.join(bin_dir, "tusk-config-tools.py"), "w", encoding="utf-8") as f:
        f.write(tools_content)

    return tmp


class TestRule7NoViolations:
    def test_all_config_keys_in_known_keys(self):
        """No violations when every config.default.json key appears in KNOWN_KEYS."""
        keys = ["domains", "task_types", "statuses", "priorities"]
        with _make_root(config_keys=keys, known_keys=keys) as root:
            assert lint.rule7_config_keys_match_known_keys(root) == []

    def test_known_keys_superset_of_config_keys(self):
        """KNOWN_KEYS may contain extra keys not in config — that is allowed."""
        config_keys = ["domains", "statuses"]
        known_keys = ["domains", "statuses", "domain_test_commands"]
        with _make_root(config_keys=config_keys, known_keys=known_keys) as root:
            assert lint.rule7_config_keys_match_known_keys(root) == []

    def test_missing_config_file_returns_empty(self):
        """If config.default.json does not exist the rule is a no-op."""
        with tempfile.TemporaryDirectory() as root:
            bin_dir = os.path.join(root, "bin")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "tusk-config-tools.py"), "w") as f:
                f.write("KNOWN_KEYS = {'domains'}\n")
            assert lint.rule7_config_keys_match_known_keys(root) == []

    def test_missing_config_tools_returns_empty(self):
        """If tusk-config-tools.py does not exist the rule is a no-op."""
        with tempfile.TemporaryDirectory() as root:
            cfg = {"domains": []}
            with open(os.path.join(root, "config.default.json"), "w") as f:
                json.dump(cfg, f)
            assert lint.rule7_config_keys_match_known_keys(root) == []

    def test_empty_config_no_violations(self):
        """An empty config.default.json ({}) produces no violations."""
        known_keys = ["domains", "statuses"]
        with _make_root(config_keys=[], known_keys=known_keys) as root:
            assert lint.rule7_config_keys_match_known_keys(root) == []


class TestRule7Violations:
    def test_unknown_key_flagged(self):
        """A key in config.default.json that is absent from KNOWN_KEYS triggers a violation."""
        config_keys = ["domains", "mystery_key"]
        known_keys = ["domains"]
        with _make_root(config_keys=config_keys, known_keys=known_keys) as root:
            violations = lint.rule7_config_keys_match_known_keys(root)
        assert len(violations) == 1
        assert "mystery_key" in violations[0]

    def test_multiple_unknown_keys_all_flagged(self):
        """Each unknown key produces its own violation entry."""
        config_keys = ["domains", "alpha", "beta"]
        known_keys = ["domains"]
        with _make_root(config_keys=config_keys, known_keys=known_keys) as root:
            violations = lint.rule7_config_keys_match_known_keys(root)
        assert len(violations) == 2
        reported = " ".join(violations)
        assert "alpha" in reported
        assert "beta" in reported

    def test_violation_message_mentions_config_tools(self):
        """The violation message references bin/tusk-config-tools.py."""
        config_keys = ["domains", "unknown_key"]
        known_keys = ["domains"]
        with _make_root(config_keys=config_keys, known_keys=known_keys) as root:
            violations = lint.rule7_config_keys_match_known_keys(root)
        assert any("tusk-config-tools.py" in v for v in violations)
