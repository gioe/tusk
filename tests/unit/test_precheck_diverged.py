"""Unit tests for test-precheck's diverged-from-default signal (issue #1082).

Builds a real bare origin + clone so the HEAD-vs-origin/<default> three-dot diff
exercises actual git plumbing. Covers: divergence detected when the default
branch is ahead, no divergence when HEAD is up to date, --paths scoping, the
pre_existing=false short-circuit, and the no-origin no-op.
"""

import importlib.util
import json
import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_test_precheck", os.path.join(BIN, "tusk-test-precheck.py")
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture()
def diverged_repo(tmp_path):
    """A clone whose feature HEAD is behind origin/main, which added test_x.py.

    Returns the work-tree path. origin/main has one commit (touching test_x.py
    and notes.md) that the checked-out feature HEAD lacks.
    """
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", "-b", "main", str(origin)], str(tmp_path))

    work = tmp_path / "work"
    _git(["clone", str(origin), str(work)], str(tmp_path))
    _git(["config", "user.email", "t@t.t"], str(work))
    _git(["config", "user.name", "t"], str(work))

    (work / "seed.txt").write_text("seed\n")
    _git(["add", "seed.txt"], str(work))
    _git(["commit", "-m", "A"], str(work))
    _git(["push", "origin", "main"], str(work))

    # Feature branch at A.
    _git(["checkout", "-b", "feature"], str(work))

    # Advance main on origin: add test_x.py + notes.md (commit B).
    _git(["checkout", "main"], str(work))
    (work / "test_x.py").write_text("def test_x():\n    assert True\n")
    (work / "notes.md").write_text("notes\n")
    _git(["add", "test_x.py", "notes.md"], str(work))
    _git(["commit", "-m", "B fix"], str(work))
    _git(["push", "origin", "main"], str(work))

    # Back to feature (HEAD = A, behind origin/main = B).
    _git(["checkout", "feature"], str(work))
    return str(work)


@pytest.fixture()
def uptodate_repo(tmp_path):
    """A clone whose HEAD == origin/main (no divergence)."""
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", "-b", "main", str(origin)], str(tmp_path))
    work = tmp_path / "work"
    _git(["clone", str(origin), str(work)], str(tmp_path))
    _git(["config", "user.email", "t@t.t"], str(work))
    _git(["config", "user.name", "t"], str(work))
    (work / "seed.txt").write_text("seed\n")
    _git(["add", "seed.txt"], str(work))
    _git(["commit", "-m", "A"], str(work))
    _git(["push", "origin", "main"], str(work))
    return str(work)


def test_divergence_detected_when_default_ahead(diverged_repo):
    diverged, paths = mod._compute_divergence(diverged_repo, BIN, None)
    assert diverged is True
    assert "test_x.py" in paths
    assert "notes.md" in paths


def test_no_divergence_when_head_up_to_date(uptodate_repo):
    diverged, paths = mod._compute_divergence(uptodate_repo, BIN, None)
    assert diverged is False
    assert paths == []


def test_paths_scoping_limits_diff(diverged_repo):
    # Scope to a path the default branch did NOT touch → no divergence reported.
    diverged, paths = mod._compute_divergence(diverged_repo, BIN, ["seed.txt"])
    assert diverged is False
    assert paths == []
    # Scope to a path it DID touch → divergence reported.
    diverged, paths = mod._compute_divergence(diverged_repo, BIN, ["test_x.py"])
    assert diverged is True
    assert paths == ["test_x.py"]


def test_no_origin_is_noop(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(["init", "-b", "main", str(repo)], str(tmp_path))
    _git(["config", "user.email", "t@t.t"], str(repo))
    _git(["config", "user.name", "t"], str(repo))
    (repo / "a.txt").write_text("a\n")
    _git(["add", "a.txt"], str(repo))
    _git(["commit", "-m", "A"], str(repo))
    diverged, paths = mod._compute_divergence(str(repo), BIN, None)
    assert diverged is False
    assert paths == []


def test_emit_verdict_pre_existing_true_sets_field(diverged_repo, capsys):
    rc = mod._emit_verdict(diverged_repo, BIN, "false", 1, False, None)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pre_existing"] is True
    assert out["diverged_from_default"] is True
    assert "test_x.py" in out["diverged_paths"]


def test_emit_verdict_pre_existing_false_short_circuits(diverged_repo, capsys):
    # exit_code 0 → pre_existing False → divergence check must NOT run even
    # though origin/main is ahead.
    rc = mod._emit_verdict(diverged_repo, BIN, "true", 0, False, None)
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["pre_existing"] is False
    assert out["diverged_from_default"] is False
    assert out["diverged_paths"] == []
    assert "may already be fixed upstream" not in captured.err


def test_emit_verdict_warns_on_stderr_when_diverged(diverged_repo, capsys):
    mod._emit_verdict(diverged_repo, BIN, "false", 1, False, None)
    captured = capsys.readouterr()
    assert "may already be fixed upstream" in captured.err


def test_emit_verdict_ignores_unrelated_upstream_divergence(diverged_repo, capsys):
    failure_output = (
        "FAILED tests/test_tusk_env.py::test_missing_bin - AssertionError\n"
        "1 failed in 0.10s\n"
    )

    mod._emit_verdict(
        diverged_repo, BIN, "pytest", 1, False, None,
        failure_output=failure_output,
    )

    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["pre_existing"] is True
    assert out["diverged_from_default"] is False
    assert out["diverged_paths"] == []
    assert "may already be fixed upstream" not in captured.err


def test_emit_verdict_reports_overlap_with_failing_test(diverged_repo, capsys):
    failure_output = (
        "FAILED test_x.py::test_x - AssertionError\n"
        "1 failed in 0.10s\n"
    )

    mod._emit_verdict(
        diverged_repo, BIN, "pytest", 1, False, None,
        failure_output=failure_output,
    )

    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["pre_existing"] is True
    assert out["diverged_from_default"] is True
    assert out["diverged_paths"] == ["test_x.py"]
    assert "may already be fixed upstream" in captured.err
