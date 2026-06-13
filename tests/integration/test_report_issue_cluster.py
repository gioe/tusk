"""Tests for cluster labels on `tusk report-issue`."""

import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run_report_issue(*args):
    env = {**os.environ, "TUSK_QUIET": "1"}
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    return subprocess.run(
        [TUSK_BIN, "report-issue", "--title", "cluster test", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_report_issue_dry_run_defaults_to_triage_cluster():
    result = _run_report_issue("--dry-run")

    assert result.returncode == 0, result.stderr
    assert "--label instance-feedback" in result.stdout
    assert "--label cluster:triage-needed" in result.stdout


def test_report_issue_dry_run_accepts_explicit_cluster():
    result = _run_report_issue("--cluster", "worktree", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "--label instance-feedback" in result.stdout
    assert "--label cluster:worktree" in result.stdout


def test_report_issue_dry_run_accepts_arbitrary_cluster():
    """TASK-424 (issue #828) dropped the hardcoded cluster enum — any
    cluster name is passed through so new GitHub labels work immediately
    without a CLI edit. (Replaces the stale rejects-unknown-cluster test
    that asserted the removed enum.)"""
    result = _run_report_issue("--cluster", "nonsense", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "--label cluster:nonsense" in result.stdout
