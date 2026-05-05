"""Regression test for issue #675: grep-based code criteria fail on multi-line
Python signatures.

skills/create-task/SKILL.md previously recommended `grep -q` for code-type
verification specs. Line-based grep silently fails on Python signatures that
span multiple lines (the norm under PEP8/black), causing `tusk criteria done`
to block legitimate fixes (concrete failure observed in TASK-1935 → issue #675).

The skill now recommends an `ast.parse` Python one-liner for "function accepts
param" assertions on Python files. This test pins three contracts:

  - The recommended AST pattern exits 0 on a multi-line signature when the
    param IS present.
  - It exits nonzero when the param is absent.
  - The legacy `grep -qE 'def fn(...param)'` pattern exits 1 on the multi-line
    signature even though the param is present — documenting the bug we fixed.
  - The legacy grep pattern still works on single-line signatures (no
    regression on the existing rubric examples).
"""

import subprocess
import textwrap

# Multi-line signature with the target param present — the case the legacy
# grep pattern silently fails on.
MULTILINE_WITH_PARAM = textwrap.dedent("""\
    async def fetch_json(
        session,
        url,
        scraper_key=None,
    ):
        pass
""")

# Same signature with the target param removed.
MULTILINE_WITHOUT_PARAM = textwrap.dedent("""\
    async def fetch_json(
        session,
        url,
    ):
        pass
""")

# Single-line signature — exists to confirm the existing single-line rubric
# example is not regressed by the new guidance.
SINGLE_LINE_WITH_PARAM = "def fetch_json(session, url, scraper_key=None): pass\n"

# The recommended AST one-liner from skills/create-task/SKILL.md. Argv form
# avoids interpolating user data into Python source.
AST_SPEC_TEMPLATE = (
    'python3 -c "import ast,sys; t=ast.parse(open(sys.argv[1]).read()); '
    "assert any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) "
    "and n.name==sys.argv[2] "
    "and any(a.arg==sys.argv[3] "
    "for a in n.args.args+n.args.kwonlyargs+n.args.posonlyargs) "
    'for n in ast.walk(t))" {path} fetch_json scraper_key'
)

# The legacy grep pattern from the issue body — pinned here to document the
# bug, not as a recommended approach.
GREP_SPEC_TEMPLATE = "grep -qE 'def fetch_json\\([^)]*scraper_key' {path}"


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def _run(spec):
    return subprocess.run(spec, shell=True, capture_output=True).returncode


def test_ast_pattern_passes_on_multiline_with_param(tmp_path):
    path = _write(tmp_path, "ml_with.py", MULTILINE_WITH_PARAM)
    assert _run(AST_SPEC_TEMPLATE.format(path=path)) == 0


def test_ast_pattern_fails_on_multiline_without_param(tmp_path):
    path = _write(tmp_path, "ml_without.py", MULTILINE_WITHOUT_PARAM)
    assert _run(AST_SPEC_TEMPLATE.format(path=path)) != 0


def test_ast_pattern_passes_on_single_line_with_param(tmp_path):
    path = _write(tmp_path, "single.py", SINGLE_LINE_WITH_PARAM)
    assert _run(AST_SPEC_TEMPLATE.format(path=path)) == 0


def test_legacy_grep_pattern_fails_on_multiline_with_param(tmp_path):
    """Pin the bug: line-based grep exits 1 on a multi-line signature
    even though the param is present in the file. This is the failure mode
    that caused issue #675; if a future grep change ever makes this pass,
    the rubric warning can be revisited."""
    path = _write(tmp_path, "ml_with.py", MULTILINE_WITH_PARAM)
    assert _run(GREP_SPEC_TEMPLATE.format(path=path)) == 1


def test_legacy_grep_pattern_passes_on_single_line(tmp_path):
    """The single-line rubric example is unchanged: grep still answers
    correctly when the signature fits on one line. This guards against
    inadvertently breaking the existing rubric guidance for the common case."""
    path = _write(tmp_path, "single.py", SINGLE_LINE_WITH_PARAM)
    assert _run(GREP_SPEC_TEMPLATE.format(path=path)) == 0
