"""Tests for report-issue filing-time guards (issues #1087 / #1040).

``tusk report-issue`` used to file unconditionally, so five sessions filed
five near-identical issues for one root cause (#1087), and stale-version
installations filed already-fixed bugs (#1040). The filing path now runs two
best-effort guards: a title-similarity dedupe against open instance-feedback
issues (match -> occurrence comment instead of a duplicate; ``--force``
overrides) and a local-vs-latest VERSION comparison (behind -> stderr warning
plus a body annotation). gh failures never block filing.

The ``gh`` CLI is stubbed via PATH so every test is hermetic: the stub
serves canned ``issue list`` / ``api .../VERSION`` responses and records
``issue comment`` / ``issue create`` argv lines to a log file.
"""

import base64
import json
import os
import stat
import subprocess

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")

OPEN_ISSUES = [
    {"number": 901, "title": "skill-run cost estimation returns zero for claude-fable-5"},
    {"number": 902, "title": "task-worktree create derives bogus sparse cone entries"},
]


def _write_gh_stub(tmp_path, *, latest_version="", list_exit=0, comment_exit=0):
    """A fake ``gh`` that serves canned data and logs mutating calls."""
    log = tmp_path / "gh-calls.log"
    issues_json = json.dumps(OPEN_ISSUES)
    version_b64 = base64.b64encode(f"{latest_version}\n".encode()).decode() if latest_version else ""
    stub = tmp_path / "bin" / "gh"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/bash\n"
        f"LOG={json.dumps(str(log))}\n"
        'case "$1 $2" in\n'
        '  "issue list")\n'
        f"    if [ {list_exit} -ne 0 ]; then exit {list_exit}; fi\n"
        f"    printf '%s' {json.dumps(issues_json)}\n"
        "    ;;\n"
        '  "api repos/gioe/tusk/contents/VERSION"*|"api "*)\n'
        f"    if [ -z {json.dumps(version_b64)} ]; then exit 1; fi\n"
        f"    printf '%s' {json.dumps(version_b64)}\n"
        "    ;;\n"
        '  "issue comment")\n'
        '    echo "comment $*" >> "$LOG"\n'
        f"    exit {comment_exit}\n"
        "    ;;\n"
        '  "issue create")\n'
        '    echo "create $*" >> "$LOG"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub.parent, log


def _run(args, stub_bin, cwd):
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["TUSK_QUIET"] = "1"
    env.pop("TUSK_PROJECT", None)
    env.pop("TUSK_DB", None)
    return subprocess.run(
        [TUSK_BIN, "report-issue", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _log_lines(log):
    if not os.path.exists(log):
        return []
    return [l for l in open(log, encoding="utf-8").read().splitlines() if l.strip()]


def test_similar_title_appends_occurrence_comment_instead_of_filing(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path)
    result = _run(
        ["--title", "skill-run cost estimation returns zero for claude-fable-5 model",
         "--context", "seen again on a fresh install"],
        stub_bin, tmp_path,
    )

    assert result.returncode == 0, result.stderr
    # Explanation goes to stderr; stdout carries only the matched issue's
    # URL so ISSUE_URL=$(tusk report-issue ...) callers keep working.
    assert "Matched existing open issue #901" in result.stderr
    assert "--force" in result.stderr
    assert result.stdout.strip() == "https://github.com/gioe/tusk/issues/901"
    lines = _log_lines(log)
    assert any(l.startswith("comment 901") or l.startswith("comment issue") or "comment" in l for l in lines), lines
    assert not any(l.startswith("create") for l in lines), (
        f"no issue should be created on a dedupe match; log={lines}"
    )


def test_force_files_new_issue_despite_match(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path)
    result = _run(
        ["--title", "skill-run cost estimation returns zero for claude-fable-5 model",
         "--force"],
        stub_bin, tmp_path,
    )

    assert result.returncode == 0, result.stderr
    lines = _log_lines(log)
    assert any(l.startswith("create") for l in lines), f"--force must file; log={lines}"
    assert not any("comment" in l for l in lines)


def test_unrelated_title_files_normally(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path)
    result = _run(["--title", "completely unrelated new failure shape"], stub_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    lines = _log_lines(log)
    assert any(l.startswith("create") for l in lines), lines


def test_gh_list_failure_never_blocks_filing(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path, list_exit=1)
    result = _run(
        ["--title", "skill-run cost estimation returns zero for claude-fable-5 model"],
        stub_bin, tmp_path,
    )

    assert result.returncode == 0, result.stderr
    lines = _log_lines(log)
    assert any(l.startswith("create") for l in lines), (
        f"dedupe-fetch failure must fall through to filing; log={lines}"
    )


def test_comment_failure_falls_through_to_filing(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path, comment_exit=1)
    result = _run(
        ["--title", "skill-run cost estimation returns zero for claude-fable-5 model"],
        stub_bin, tmp_path,
    )

    assert result.returncode == 0, result.stderr
    lines = _log_lines(log)
    assert any(l.startswith("create") for l in lines), (
        f"comment failure must fall through to filing; log={lines}"
    )


def test_behind_version_warns_and_annotates_body(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path, latest_version="99999")
    result = _run(["--title", "completely unrelated new failure shape"], stub_bin, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "run 'tusk upgrade'" in result.stderr
    lines = _log_lines(log)
    assert any(l.startswith("create") for l in lines), lines
    # The --body argument is multi-line, so the annotation lands on a later
    # line of the recorded argv — search the whole log.
    log_text = open(log, encoding="utf-8").read()
    assert "filer is behind" in log_text
    assert "latest: 99999" in log_text


def test_dry_run_reports_dedupe_decision_without_acting(tmp_path):
    stub_bin, log = _write_gh_stub(tmp_path)
    result = _run(
        ["--title", "skill-run cost estimation returns zero for claude-fable-5 model",
         "--dry-run"],
        stub_bin, tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "Dedupe: would append an occurrence comment" in result.stdout
    assert "#901" in result.stdout
    assert _log_lines(log) == [], "dry-run must not comment or create"
