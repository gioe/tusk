"""Integration tests for the tusk-commit lint gate (TASK-54).

Covers:
- Blocking path: a non-advisory lint violation aborts ``tusk commit`` with
  the new exit code 6 and no commit is created.
- Bypass path: ``--skip-lint`` and ``--skip-verify`` both bypass the gate.
- Advisory-only rules still print their findings but do NOT block — regression
  guard for criterion 242.
- Quiet output: ``tusk-lint.py --quiet`` omits passing rules and prints only
  rules with violations.
"""

import os
import subprocess
import textwrap

import pytest


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")
TUSK_COMMIT_PY = os.path.join(REPO_ROOT, "bin", "tusk-commit.py")
TUSK_LINT_PY = os.path.join(REPO_ROOT, "bin", "tusk-lint.py")
CONFIG_DEFAULT = os.path.join(REPO_ROOT, "config.default.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(repo: str) -> None:
    """Initialise a bare-bones git repo with a root commit so rev-parse works."""
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Test"], check=True
    )
    seed = os.path.join(repo, "README.md")
    with open(seed, "w") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", "root"], check=True
    )


def _plant_blocking_violation(repo: str) -> None:
    """Plant a SKILL.md with no frontmatter — triggers blocking Rule 11."""
    skills_dir = os.path.join(repo, "skills", "broken")
    os.makedirs(skills_dir, exist_ok=True)
    with open(os.path.join(skills_dir, "SKILL.md"), "w") as f:
        # No YAML frontmatter → Rule 11 violation (non-advisory).
        f.write("just a body, no frontmatter\n")


def _run_commit(repo: str, *extra_args: str, env_extra: dict | None = None):
    """Invoke tusk-commit.py as a subprocess against ``repo``."""
    env = os.environ.copy()
    # Pin subprocess invocations to the fake repo so that nested `bin/tusk
    # lint` calls lint the fixture, not the real tusk source tree.
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["python3", TUSK_COMMIT_PY, repo, CONFIG_DEFAULT, "999", "msg", *extra_args],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
    )


# ---------------------------------------------------------------------------
# Quiet output (criterion 243 regression)
# ---------------------------------------------------------------------------


class TestLintQuietOutput:
    def test_default_is_terse_and_verbose_is_full(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)

        default = subprocess.run(
            ["python3", TUSK_LINT_PY, repo],
            capture_output=True, text=True,
        )
        verbose = subprocess.run(
            ["python3", TUSK_LINT_PY, repo, "--verbose"],
            capture_output=True, text=True,
        )
        quiet = subprocess.run(
            ["python3", TUSK_LINT_PY, repo, "--quiet"],
            capture_output=True, text=True,
        )

        # Verbose output shows per-rule PASS lines for clean rules.
        assert "PASS — no violations" in verbose.stdout
        assert "=== Lint Conventions Report ===" in verbose.stdout

        # Default output drops per-rule detail but prints a one-line OK summary.
        assert "PASS — no violations" not in default.stdout
        assert "=== Lint Conventions Report ===" not in default.stdout
        assert default.stdout.startswith("OK —") or "\nOK —" in default.stdout

        # Quiet output is entirely silent on clean success.
        assert quiet.stdout == ""

    def test_quiet_still_prints_violations(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _plant_blocking_violation(repo)

        result = subprocess.run(
            ["python3", TUSK_LINT_PY, repo, "--quiet"],
            capture_output=True, text=True,
        )

        # The violation rule is still printed verbatim and the exit code fires.
        assert result.returncode == 1
        assert "Rule 11" in result.stdout
        assert "WARN" in result.stdout
        assert "PASS" not in result.stdout


# ---------------------------------------------------------------------------
# Blocking / bypass (criteria 241, 244)
# ---------------------------------------------------------------------------


class TestCommitLintGate:
    def test_blocking_lint_aborts_with_exit_6(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _plant_blocking_violation(repo)

        # Commit a new file so we can tell whether the commit landed.
        target = os.path.join(repo, "new.txt")
        with open(target, "w") as f:
            f.write("payload\n")

        result = _run_commit(repo, target)

        assert result.returncode == 6, (
            f"expected exit 6, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "aborting commit" in (result.stdout + result.stderr)

        # No commit was created — git log length unchanged at 1 (the seed).
        log = subprocess.run(
            ["git", "-C", repo, "log", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        assert len(log.stdout.strip().splitlines()) == 1

    def test_skip_lint_bypasses_gate(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _plant_blocking_violation(repo)
        target = os.path.join(repo, "new.txt")
        with open(target, "w") as f:
            f.write("payload\n")

        result = _run_commit(repo, target, "--skip-lint")

        assert result.returncode == 0, (
            f"expected success with --skip-lint, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        # Commit landed.
        log = subprocess.run(
            ["git", "-C", repo, "log", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        assert len(log.stdout.strip().splitlines()) == 2

    def test_skip_verify_bypasses_gate(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)
        _plant_blocking_violation(repo)
        target = os.path.join(repo, "new.txt")
        with open(target, "w") as f:
            f.write("payload\n")

        result = _run_commit(repo, target, "--skip-verify")

        assert result.returncode == 0, (
            f"expected success with --skip-verify, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# Advisory-doesn't-block regression (criterion 242)
# ---------------------------------------------------------------------------


class TestAdvisoryDoesNotBlock:
    def test_clean_repo_commits_cleanly(self, tmp_path):
        """No violations → commit proceeds without a non-zero lint exit."""
        repo = str(tmp_path / "repo")
        _git_init(repo)
        target = os.path.join(repo, "new.txt")
        with open(target, "w") as f:
            f.write("payload\n")

        result = _run_commit(repo, target)

        assert result.returncode == 0, (
            f"expected exit 0 on clean repo, got {result.returncode}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_advisory_only_violation_does_not_block(self, tmp_path):
        """A plain advisory (VERSION bump missing) warns but must not block.

        We simulate an advisory by invoking tusk-lint.py directly against a
        repo whose state trips only the ``_version_bump_check`` advisory
        rules (Rules 13/20) — no blocking rule fires.  The lint exit code
        is 0 in that case, confirming advisory rules never contribute to
        the non-zero exit that would abort ``tusk commit``.
        """
        repo = str(tmp_path / "repo")
        _git_init(repo)

        # Plant a bin/tusk-*.py change so Rule 13 fires (advisory).  Silence
        # the other blocking rules that would otherwise fire on a near-empty
        # bin/ by including the script name in bin/tusk (Rule 8) and creating
        # matching MANIFEST files (Rules 18, 19).
        bin_dir = os.path.join(repo, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        tusk_shim = os.path.join(bin_dir, "tusk")
        # The shim must be executable because _db_path_from_root runs it.
        # The string "tusk-sample.py" inside it satisfies Rule 8's dispatcher
        # check (the rule does a substring search, not a parse).
        with open(tusk_shim, "w") as f:
            f.write("#!/bin/bash\n# references tusk-sample.py\nexit 0\n")
        os.chmod(tusk_shim, 0o755)
        with open(os.path.join(bin_dir, "tusk-sample.py"), "w") as f:
            f.write("# sample\n")
        # Rule 18 reads bin/dist-excluded.txt to filter dist-excluded scripts.
        # Empty file means "nothing excluded" — all bin/tusk-*.py scripts must
        # appear in MANIFEST, which matches our fixture.
        with open(os.path.join(bin_dir, "dist-excluded.txt"), "w") as f:
            f.write("")
        with open(os.path.join(repo, "VERSION"), "w") as f:
            f.write("1\n")
        # MANIFEST + .claude/tusk-manifest.json must match the expected
        # distributed file set to satisfy Rules 18 and 19.  install.sh copies
        # VERSION and config.default.json by default; we include the sample
        # script too.  Rule 18 also requires config.default.json/pricing.json
        # entries, which we list even though the files aren't present — both
        # rules only compare the JSON file sets, not on-disk presence.
        manifest_entries = [
            ".claude/bin/tusk",
            ".claude/bin/tusk-sample.py",
            ".claude/bin/config.default.json",
            ".claude/bin/VERSION",
            ".claude/bin/pricing.json",
        ]
        import json as _json
        with open(os.path.join(repo, "MANIFEST"), "w") as f:
            _json.dump(manifest_entries, f)
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        with open(os.path.join(repo, ".claude", "tusk-manifest.json"), "w") as f:
            _json.dump(manifest_entries, f)
        subprocess.run(
            [
                "git", "-C", repo, "add",
                "bin/tusk", "bin/tusk-sample.py", "bin/dist-excluded.txt",
                "VERSION", "MANIFEST", ".claude/tusk-manifest.json",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo, "commit", "-q", "-m", "add scripts"], check=True
        )
        # Plant an intervening commit so VERSION is no longer on HEAD; otherwise
        # _version_bump_check's just-bumped guard (Issue #631) suppresses Rule
        # 13 because the prior commit was the VERSION bump.
        with open(os.path.join(repo, "README.md"), "a") as f:
            f.write("filler\n")
        subprocess.run(
            ["git", "-C", repo, "add", "README.md"], check=True
        )
        subprocess.run(
            ["git", "-C", repo, "commit", "-q", "-m", "intervening change"], check=True
        )
        # Now modify tusk-sample.py WITHOUT bumping VERSION — Rule 13 (advisory).
        with open(os.path.join(bin_dir, "tusk-sample.py"), "a") as f:
            f.write("# change\n")

        result = subprocess.run(
            ["python3", TUSK_LINT_PY, repo, "--quiet"],
            capture_output=True, text=True,
        )

        # Advisory-only: exit 0 (no blocking violations), yet WARN printed.
        assert result.returncode == 0, (
            f"advisory-only lint must not exit non-zero; "
            f"got {result.returncode}.\nstdout={result.stdout}"
        )
        assert "[ADVISORY]" in result.stdout


# ---------------------------------------------------------------------------
# DB isolation (Issue #633)
# ---------------------------------------------------------------------------


class TestLintDBIsolation:
    """Regression for Issue #633: tusk-lint must not leak the source repo's
    DB into a tmp_path fixture via the bare-``tusk``-on-PATH fallback in
    ``_db_path_from_root``.
    """

    def test_path_fallback_db_outside_root_is_ignored(self, tmp_path):
        """A PATH-resolved tusk binary whose DB lives outside ``<root>/tusk/``
        must be rejected — DB-backed rules (Rule 15 here) stay silent.

        Without the fix, Rule 15 reads the planted external DB (which has 2+
        criteria sharing one commit hash) and pollutes a clean fixture's lint
        output, breaking ``test_default_is_terse_and_verbose_is_full`` whenever
        the dev's source repo is in that state mid-session.
        """
        import sqlite3

        repo = str(tmp_path / "repo")
        _git_init(repo)  # No bin/tusk shim — forces the PATH fallback.

        # Plant an external DB with a Rule 15 trigger: an In Progress task
        # whose two completed, non-deferred criteria share one commit hash.
        ext_dir = tmp_path / "external" / "tusk"
        ext_dir.mkdir(parents=True)
        ext_db = ext_dir / "tasks.db"
        conn = sqlite3.connect(str(ext_db))
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                summary TEXT,
                status TEXT
            );
            CREATE TABLE acceptance_criteria (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                is_completed INTEGER,
                is_deferred INTEGER,
                commit_hash TEXT
            );
            INSERT INTO tasks VALUES (9991, 'leak demo task', 'In Progress');
            INSERT INTO acceptance_criteria
                VALUES (1, 9991, 1, 0, 'deadbeef');
            INSERT INTO acceptance_criteria
                VALUES (2, 9991, 1, 0, 'deadbeef');
            """
        )
        conn.commit()
        conn.close()

        # Fake `tusk` on PATH that prints the external DB path on `tusk path`.
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_tusk = fake_bin / "tusk"
        fake_tusk.write_text(
            f'#!/bin/bash\nif [ "$1" = "path" ]; then echo "{ext_db}"; fi\n'
        )
        fake_tusk.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        # Strip TUSK_PROJECT/TUSK_DB so they can't override the fallback path.
        env.pop("TUSK_PROJECT", None)
        env.pop("TUSK_DB", None)

        result = subprocess.run(
            ["python3", TUSK_LINT_PY, repo, "--quiet"],
            capture_output=True, text=True, env=env,
        )

        # The external DB must NOT contribute to the lint output.
        assert "Rule 15" not in result.stdout, (
            f"Rule 15 leaked from external DB: stdout={result.stdout!r}"
        )
        assert "leak demo task" not in result.stdout
        assert "TASK-9991" not in result.stdout
        # And the clean-fixture invariant from
        # test_default_is_terse_and_verbose_is_full holds.
        assert result.stdout == "", (
            f"--quiet on a clean fixture must produce no output; "
            f"got {result.stdout!r}"
        )
