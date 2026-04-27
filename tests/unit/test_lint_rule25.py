"""Unit tests for rule25_subcommand_dispatcher_drift in tusk-lint.py.

Covers the clean path (dispatcher, candidates list, and Usage message all in
sync) and each drift mode the rule detects: dispatcher entries missing from
candidates, dispatcher entries missing from Usage, and stale entries left
behind in candidates or Usage after a subcommand was removed from the
dispatcher.
"""

import importlib.util
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_lint",
    os.path.join(REPO_ROOT, "bin", "tusk-lint.py"),
)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _make_root(dispatcher: list[str], candidates: list[str], usage: list[str]) -> tempfile.TemporaryDirectory:
    """Write a synthetic bin/tusk containing a dispatcher case, candidates list, and Usage message."""
    tmp = tempfile.TemporaryDirectory()
    bin_dir = os.path.join(tmp.name, "bin")
    os.makedirs(bin_dir)

    case_arms = "\n".join(f"  {name}) cmd_{name.replace('-', '_')} ;;" for name in dispatcher)
    cand_lines = ",".join(f"'{c}'" for c in candidates)
    usage_pipes = "|".join(usage + ['"SQL ..."'])

    content = (
        "#!/usr/bin/env bash\n"
        "# synthetic bin/tusk fixture for rule25 tests\n"
        "\n"
        "candidates = [\n"
        f"  {cand_lines},\n"
        "]\n"
        "\n"
        'case "${1:-}" in\n'
        f"{case_arms}\n"
        f'  "")     echo "Usage: tusk {{{usage_pipes}}}" >&2; exit 1 ;;\n'
        '  *)      cmd_query "$@" ;;\n'
        "esac\n"
    )

    with open(os.path.join(bin_dir, "tusk"), "w", encoding="utf-8") as f:
        f.write(content)
    return tmp


class TestRule25NoViolations:
    def test_all_three_lists_in_sync(self):
        names = ["init", "lint", "task-start"]
        with _make_root(dispatcher=names, candidates=names, usage=names) as tmp:
            assert lint.rule25_subcommand_dispatcher_drift(tmp) == []

    def test_missing_tusk_returns_empty(self):
        with tempfile.TemporaryDirectory() as root:
            assert lint.rule25_subcommand_dispatcher_drift(root) == []

    def test_missing_dispatcher_returns_empty(self):
        with tempfile.TemporaryDirectory() as root:
            bin_dir = os.path.join(root, "bin")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "tusk"), "w") as f:
                f.write("#!/usr/bin/env bash\necho hello\n")
            assert lint.rule25_subcommand_dispatcher_drift(root) == []


class TestRule25Violations:
    def test_dispatcher_entry_missing_from_candidates(self):
        dispatcher = ["init", "lint", "newcmd"]
        candidates = ["init", "lint"]
        usage = ["init", "lint", "newcmd"]
        with _make_root(dispatcher=dispatcher, candidates=candidates, usage=usage) as tmp:
            violations = lint.rule25_subcommand_dispatcher_drift(tmp)
        joined = "\n".join(violations)
        assert "'newcmd' in dispatcher but missing from candidates" in joined
        assert "missing from Usage" not in joined

    def test_dispatcher_entry_missing_from_usage(self):
        dispatcher = ["init", "lint", "newcmd"]
        candidates = ["init", "lint", "newcmd"]
        usage = ["init", "lint"]
        with _make_root(dispatcher=dispatcher, candidates=candidates, usage=usage) as tmp:
            violations = lint.rule25_subcommand_dispatcher_drift(tmp)
        joined = "\n".join(violations)
        assert "'newcmd' in dispatcher but missing from Usage" in joined
        assert "missing from candidates" not in joined

    def test_stale_candidate_flagged(self):
        dispatcher = ["init", "lint"]
        candidates = ["init", "lint", "removedcmd"]
        usage = ["init", "lint"]
        with _make_root(dispatcher=dispatcher, candidates=candidates, usage=usage) as tmp:
            violations = lint.rule25_subcommand_dispatcher_drift(tmp)
        joined = "\n".join(violations)
        assert "'removedcmd' in candidates" in joined
        assert "stale entry" in joined

    def test_stale_usage_entry_flagged(self):
        dispatcher = ["init", "lint"]
        candidates = ["init", "lint"]
        usage = ["init", "lint", "dag"]
        with _make_root(dispatcher=dispatcher, candidates=candidates, usage=usage) as tmp:
            violations = lint.rule25_subcommand_dispatcher_drift(tmp)
        joined = "\n".join(violations)
        assert "'dag' in Usage message" in joined
        assert "stale entry" in joined

    def test_three_drifts_all_reported(self):
        dispatcher = ["init", "lint", "newcmd"]
        candidates = ["init", "lint"]
        usage = ["init", "lint", "ghost"]
        with _make_root(dispatcher=dispatcher, candidates=candidates, usage=usage) as tmp:
            violations = lint.rule25_subcommand_dispatcher_drift(tmp)
        joined = "\n".join(violations)
        assert "'newcmd' in dispatcher but missing from candidates" in joined
        assert "'newcmd' in dispatcher but missing from Usage" in joined
        assert "'ghost' in Usage message" in joined
