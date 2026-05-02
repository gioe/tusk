"""Tests for the tusk typed-criteria-build helper.

The helper exists to collapse the brittle shell-quoting dance for embedding a
test spec into --typed-criteria JSON. It must round-trip arbitrary specs
(single quotes, double quotes, backslashes, newlines, control chars) through
JSON cleanly so callers never have to reinvent the escape.

This file covers two layers:

1. The pure-Python escape function (`build`) — fast, no subprocess.
2. The CLI behaviour (stdin, --spec-file, defaults, exit codes) and the
   regression for issue #639's specific reproducer — invoked via the tusk
   wrapper so the dispatcher path is exercised end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")
SCRIPT = os.path.join(BIN, "tusk-typed-criteria-build.py")
TUSK = os.path.join(BIN, "tusk")
SKILL_PATH = os.path.join(REPO_ROOT, "skills", "address-issue", "SKILL.md")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_typed_criteria_build", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _roundtrip(out: str) -> dict:
    """Parse stdout JSON; assert structure and return the dict."""
    obj = json.loads(out)
    assert set(obj.keys()) >= {"text", "type", "spec"}
    return obj


class TestBuildFunction:
    def test_plain_spec_emits_compact_json(self, mod):
        out = mod.build("pytest -q tests/unit/", "Failing test passes", "test")
        assert out == '{"text":"Failing test passes","type":"test","spec":"pytest -q tests/unit/"}'
        # No spaces around separators — agents consume this directly.
        assert ": " not in out and ", " not in out

    def test_double_quotes_escaped(self, mod):
        out = mod.build('echo "hi"', "t", "test")
        assert json.loads(out)["spec"] == 'echo "hi"'

    def test_backslashes_escaped(self, mod):
        out = mod.build(r"grep '\n' file", "t", "test")
        assert json.loads(out)["spec"] == r"grep '\n' file"

    def test_single_quotes_pass_through(self, mod):
        out = mod.build("pytest tests/test_foo.py::test_it's_broken", "t", "test")
        assert json.loads(out)["spec"] == "pytest tests/test_foo.py::test_it's_broken"

    def test_newlines_escaped(self, mod):
        out = mod.build("line1\nline2\nline3", "t", "test")
        # JSON must NOT contain a literal newline in the encoded string —
        # newlines must be escaped as \n so the result is a single line.
        assert "\n" not in out
        assert json.loads(out)["spec"] == "line1\nline2\nline3"

    def test_control_chars_escaped(self, mod):
        spec = "a\tb\rc\bd"
        out = mod.build(spec, "t", "test")
        assert json.loads(out)["spec"] == spec

    def test_unicode_preserved_as_utf8(self, mod):
        out = mod.build("echo café 🐘", "t", "test")
        # ensure_ascii=False keeps multibyte chars as UTF-8 bytes, not \uXXXX.
        assert "café" in out and "🐘" in out
        assert json.loads(out)["spec"] == "echo café 🐘"

    def test_issue_639_reproducer_roundtrips(self, mod):
        """The exact failing spec from issue #639 must round-trip cleanly."""
        spec = (
            "TEST_SPEC='cat <<EOF\nfoo \"bar\"\nEOF'; "
            'JSON="{\\"text\\":\\"t\\",\\"type\\":\\"test\\",\\"spec\\":\\"$TEST_SPEC\\"}"; '
            'printf %s "$JSON" | python3 -c "import json,sys; json.load(sys.stdin)"'
        )
        out = mod.build(spec, "Failing test passes", "test")
        # Must parse cleanly — this is the exact failure mode the helper fixes.
        parsed = json.loads(out)
        assert parsed["spec"] == spec
        assert parsed["text"] == "Failing test passes"
        assert parsed["type"] == "test"


class TestCLIInvocation:
    def test_stdin_input_strips_one_trailing_newline(self):
        # Heredocs and `printf '%s\n'` always append \n — strip exactly one.
        result = subprocess.run(
            [sys.executable, SCRIPT],
            input="cmd arg1 arg2\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert _roundtrip(result.stdout)["spec"] == "cmd arg1 arg2"

    def test_internal_newlines_preserved(self):
        result = subprocess.run(
            [sys.executable, SCRIPT],
            input="line1\nline2\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        # The trailing \n is stripped (1 char), but the internal one is kept.
        assert _roundtrip(result.stdout)["spec"] == "line1\nline2"

    def test_spec_file_argument(self, tmp_path):
        spec_file = tmp_path / "spec.txt"
        spec_file.write_text('echo "hi" && grep \\d+ file\n', encoding="utf-8")
        result = subprocess.run(
            [sys.executable, SCRIPT, "--spec-file", str(spec_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert _roundtrip(result.stdout)["spec"] == 'echo "hi" && grep \\d+ file'

    def test_default_text_and_type(self):
        result = subprocess.run(
            [sys.executable, SCRIPT],
            input="cmd",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        obj = _roundtrip(result.stdout)
        assert obj["text"] == "Failing test passes"
        assert obj["type"] == "test"

    def test_text_and_type_overrides(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--text", "Custom text", "--type", "code"],
            input="cmd",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        obj = _roundtrip(result.stdout)
        assert obj["text"] == "Custom text"
        assert obj["type"] == "code"

    def test_empty_spec_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, SCRIPT],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode != 0
        assert "empty" in result.stderr.lower()

    def test_missing_spec_file_exits_nonzero(self, tmp_path):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--spec-file", str(tmp_path / "does-not-exist")],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode != 0
        assert "spec-file" in result.stderr.lower() or "no such" in result.stderr.lower()

    def test_dispatcher_route(self):
        """Invoking through `tusk typed-criteria-build` reaches the script."""
        result = subprocess.run(
            [TUSK, "typed-criteria-build"],
            input="cmd 'mixed' \"quotes\" \\backslash\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert _roundtrip(result.stdout)["spec"] == "cmd 'mixed' \"quotes\" \\backslash"

    def test_issue_639_failing_test_now_passes(self):
        """The issue #639 reproducer's failure mode is fixed by the helper.

        Issue #639's failing test demonstrated that hand-built JSON fails to
        parse when the spec mixes ', ", and \\. Building the same JSON via the
        helper must produce output that python's json.load consumes cleanly.
        """
        spec = (
            "TEST_SPEC='cat <<EOF\nfoo \"bar\"\nEOF'; "
            'JSON="{\\"text\\":\\"t\\",\\"type\\":\\"test\\",\\"spec\\":\\"$TEST_SPEC\\"}"; '
            'printf %s "$JSON" | python3 -c "import json,sys; json.load(sys.stdin)"'
        )
        built = subprocess.run(
            [TUSK, "typed-criteria-build"],
            input=spec,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert built.returncode == 0, built.stderr

        # Pipe the helper's output through json.load (the exact verification
        # the issue's failing test performs) — must not raise.
        verify = subprocess.run(
            [sys.executable, "-c", "import json,sys; json.load(sys.stdin)"],
            input=built.stdout,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert verify.returncode == 0, (
            f"Helper output did not parse as JSON: {built.stdout!r}\nstderr: {verify.stderr}"
        )


class TestSkillDocAlignment:
    def test_skill_recommends_helper_for_quotes_or_backslash(self):
        with open(SKILL_PATH, encoding="utf-8") as f:
            text = f.read()
        # The skill must point callers at the helper for the failure mode it solves.
        assert "tusk typed-criteria-build" in text, (
            "Step 6 must recommend tusk typed-criteria-build for difficult specs"
        )
        # The recommendation must be tied to the trigger characters (issue #639).
        assert '"' in text and "\\" in text and "typed-criteria-build" in text
