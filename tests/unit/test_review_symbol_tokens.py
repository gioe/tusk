"""Unit coverage for the review symbol-extraction guard (issue #1117).

``tusk review validate-comments``'s line-symbol-mismatch guard (issue #1012)
extracts dotted code symbols from a review comment body and dismisses the
comment when the cited line does not contain that symbol but another line in
the file does. Before issue #1117 the extractor treated English prose
abbreviations like ``e.g`` / ``i.e`` as code symbols, so a correctly-anchored
``suggest`` comment whose body merely contained "(e.g. ...)" was silently
auto-dismissed.

These tests exercise ``_extract_symbol_tokens`` and ``_line_symbol_mismatch``
directly (pure functions, no DB).
"""

import importlib.util
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-review.py")

_spec = importlib.util.spec_from_file_location("tusk_review", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── _extract_symbol_tokens ──────────────────────────────────────────────


def test_prose_abbreviation_eg_is_not_a_symbol():
    """The canonical issue #1117 case: '(e.g. ...)' yields no symbols."""
    assert mod._extract_symbol_tokens("wrong (e.g. one selling stand-up) here") == []


def test_prose_abbreviation_ie_is_not_a_symbol():
    assert mod._extract_symbol_tokens("the value i.e. the count") == []


def test_prose_abbreviation_match_is_case_insensitive():
    assert mod._extract_symbol_tokens("see (E.G. above) and I.E. below") == []


def test_real_dotted_symbol_is_extracted():
    assert mod._extract_symbol_tokens("call foo.bar to fix") == ["foo.bar"]


def test_real_symbol_kept_when_prose_abbreviation_present():
    """A genuine symbol must survive even when prose abbreviations co-occur."""
    assert mod._extract_symbol_tokens(
        "clubs.visible is nullable (e.g. here)"
    ) == ["clubs.visible"]


# ── _line_symbol_mismatch ───────────────────────────────────────────────


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_prose_abbreviation_comment_not_flagged_as_mismatch(tmp_path):
    """Issue #1117: a comment whose only dotted token is a prose abbreviation
    must NOT be dismissed even though 'e.g' does not appear on the cited line.
    """
    _write(
        tmp_path,
        "mod.py",
        ["def first():", "    return 1", "    # e.g a trailing note elsewhere"],
    )
    assert (
        mod._line_symbol_mismatch(str(tmp_path), "mod.py", 1, "wrong on line 1 (e.g. nope)")
        is None
    )


def test_real_symbol_misanchored_still_flagged(tmp_path):
    """No regression to issue #1012: a real symbol named on the wrong line is
    still dismissed when it appears elsewhere in the file.
    """
    _write(
        tmp_path,
        "mod.py",
        ["def first():", "    foo.bar()", "    return 1"],
    )
    result = mod._line_symbol_mismatch(
        str(tmp_path), "mod.py", 1, "foo.bar is wrong (line 1)"
    )
    assert result is not None
    symbol, _cited = result
    assert symbol == "foo.bar"


def test_substring_token_not_treated_as_symbol_presence(tmp_path):
    """Issue #1121: a cited dotted token that only appears as a *substring* of
    an unrelated identifier elsewhere must NOT be dismissed. The literal 'a.b'
    is a substring of 'data.bar', but it is not a whole-identifier match, so the
    finding stays open for the operator.
    """
    _write(tmp_path, "f.py", ["x = 1", "    data.bar = 2"])
    assert (
        mod._line_symbol_mismatch(str(tmp_path), "f.py", 1, "a.b is wrong here")
        is None
    )


def test_whole_token_match_still_dismisses(tmp_path):
    """Issue #1121: the legitimate dismissal must still fire when the cited
    symbol appears elsewhere as a whole identifier (not a substring).
    """
    _write(tmp_path, "f.py", ["x = 1", "    data.bar = 2"])
    result = mod._line_symbol_mismatch(
        str(tmp_path), "f.py", 1, "data.bar is wrong here"
    )
    assert result is not None
    symbol, _cited = result
    assert symbol == "data.bar"
