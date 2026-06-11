"""Unit tests for file-type criterion absence checks (issue #1041).

A leading "!" on a file-type verification_spec inverts the glob check:
verification passes when the glob matches zero files. Absolute specs hit
the isabs branch of run_verification directly, so no repo root or DB is
needed; the relative-path test monkeypatches _get_repo_root.
"""

import importlib.util
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_criteria",
    os.path.join(REPO_ROOT, "bin", "tusk-criteria.py"),
)
criteria_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(criteria_mod)


def test_absence_passes_when_no_match():
    with tempfile.TemporaryDirectory() as tmp:
        result = criteria_mod.run_verification("file", f"!{tmp}/Fonts/Chivo*.ttf")
    assert result["passed"] is True, result
    assert "Absent as expected" in result["output"]


def test_absence_fails_when_match_exists():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "Fonts"))
        with open(os.path.join(tmp, "Fonts", "Chivo-Variable.ttf"), "w") as f:
            f.write("x")
        result = criteria_mod.run_verification("file", f"!{tmp}/Fonts/Chivo*.ttf")
    assert result["passed"] is False, result
    assert "Expected no files matching" in result["output"]


def test_existence_semantics_unchanged_without_bang():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "present.txt"), "w") as f:
            f.write("x")
        hit = criteria_mod.run_verification("file", f"{tmp}/present.txt")
        miss = criteria_mod.run_verification("file", f"{tmp}/absent.txt")
    assert hit["passed"] is True, hit
    assert "Found:" in hit["output"]
    assert miss["passed"] is False, miss
    assert "No files matching" in miss["output"]


def test_relative_negated_spec_anchors_at_repo_root(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(criteria_mod, "_get_repo_root", lambda: tmp)

        result = criteria_mod.run_verification("file", "!Fonts/*.ttf")
        assert result["passed"] is True, result

        os.makedirs(os.path.join(tmp, "Fonts"))
        with open(os.path.join(tmp, "Fonts", "a.ttf"), "w") as f:
            f.write("x")
        result = criteria_mod.run_verification("file", "!Fonts/*.ttf")
        assert result["passed"] is False, result


def test_bare_bang_is_invalid():
    result = criteria_mod.run_verification("file", "!")
    assert result["passed"] is False, result
    assert "Empty file pattern" in result["output"]
