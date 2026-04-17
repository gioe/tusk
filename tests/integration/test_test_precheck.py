"""Integration tests for ``tusk test-precheck`` (TASK-55).

Covers:
- Clean tree: no stash is created or popped, exit code / pre_existing flag
  reflect the raw test result.
- Dirty tree: a uniquely-named stash is pushed, the test runs against HEAD,
  and the stash is popped *by name* — never by top-of-stack.  A pre-existing
  foreign stash must remain untouched regardless of the test outcome.
- Command resolution: ``--command`` wins over ``config["test_command"]``.
"""

import json
import os
import subprocess

import pytest


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd, check=True):
    """Run a git command and return its CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _git_init(repo: str) -> None:
    """Initialise a bare-bones git repo with a seed commit."""
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("seed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "root", cwd=repo)


def _run_precheck(repo: str, *extra_args: str):
    """Invoke ``tusk test-precheck`` against ``repo`` with clean env."""
    env = os.environ.copy()
    env["TUSK_PROJECT"] = repo
    env["TUSK_QUIET"] = "1"
    return subprocess.run(
        [TUSK_BIN, "test-precheck", *extra_args],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
    )


def _parse_payload(stdout: str) -> dict:
    """Pull the JSON object out of stdout (tolerate surrounding noise)."""
    # tusk test-precheck may emit the test command's own stdout ahead of the
    # JSON line.  Parse the *last* non-empty line, which is always our payload.
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON payload in stdout:\n{stdout}")


# ---------------------------------------------------------------------------
# Clean tree (criterion 250)
# ---------------------------------------------------------------------------


class TestCleanTree:
    def test_passing_command_clean_tree_reports_not_pre_existing(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)

        result = _run_precheck(repo, "--command", "true")

        assert result.returncode == 0, result.stderr
        payload = _parse_payload(result.stdout)
        assert payload == {
            "pre_existing": False,
            "exit_code": 0,
            "test_command": "true",
            "stashed": False,
        }

    def test_failing_command_clean_tree_reports_pre_existing(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)

        result = _run_precheck(repo, "--command", "false")

        assert result.returncode == 0, result.stderr
        payload = _parse_payload(result.stdout)
        assert payload["pre_existing"] is True
        assert payload["exit_code"] == 1
        assert payload["stashed"] is False

    def test_clean_tree_does_not_pop_foreign_stash(self, tmp_path):
        """The original bug: an empty `git stash` is a no-op, then
        `git stash pop` popped a stale entry from another branch and
        silently trashed state.  test-precheck must never touch the stash
        stack when the tree is clean."""
        repo = str(tmp_path / "repo")
        _git_init(repo)

        # Plant a foreign stash that must survive the precheck.
        foreign = os.path.join(repo, "foreign.txt")
        with open(foreign, "w") as f:
            f.write("precious\n")
        _git("add", "foreign.txt", cwd=repo)
        _git("commit", "-q", "-m", "foreign file", cwd=repo)
        with open(foreign, "w") as f:
            f.write("local-edit\n")
        _git("stash", "push", "-m", "foreign-work", cwd=repo)

        before = _git("stash", "list", cwd=repo).stdout.strip()
        assert "foreign-work" in before

        # Clean tree now — precheck must not touch the stash stack.
        assert _git("status", "--porcelain", cwd=repo).stdout == ""
        result = _run_precheck(repo, "--command", "true")
        assert result.returncode == 0, result.stderr

        after = _git("stash", "list", cwd=repo).stdout.strip()
        assert after == before, (
            f"foreign stash was disturbed:\nbefore={before!r}\nafter={after!r}"
        )
        payload = _parse_payload(result.stdout)
        assert payload["stashed"] is False


# ---------------------------------------------------------------------------
# Dirty tree (criterion 249)
# ---------------------------------------------------------------------------


class TestDirtyTree:
    def test_dirty_tree_stashes_and_restores_local_changes(self, tmp_path):
        repo = str(tmp_path / "repo")
        _git_init(repo)

        # Unstaged modification to a tracked file.
        with open(os.path.join(repo, "README.md"), "w") as f:
            f.write("modified\n")
        # Plus an untracked file — both must survive the precheck.
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("untracked\n")

        result = _run_precheck(repo, "--command", "true")
        assert result.returncode == 0, result.stderr

        payload = _parse_payload(result.stdout)
        assert payload["stashed"] is True
        assert payload["pre_existing"] is False

        # Local changes are back where they started.
        assert open(os.path.join(repo, "README.md")).read() == "modified\n"
        assert open(os.path.join(repo, "new.txt")).read() == "untracked\n"
        # No leftover stash entries from our run.
        stash_list = _git("stash", "list", cwd=repo).stdout
        assert "tusk-test-precheck" not in stash_list

    def test_dirty_tree_does_not_pop_foreign_stash_on_top(self, tmp_path):
        """A foreign stash sitting on top of the stack when we push our own
        must not be popped by the precheck — we must pop *our* entry by
        reference even though it lands at stash@{0} and the foreign one is
        bumped to stash@{1}."""
        repo = str(tmp_path / "repo")
        _git_init(repo)

        # Plant a foreign stash.
        foreign = os.path.join(repo, "foreign.txt")
        with open(foreign, "w") as f:
            f.write("precious\n")
        _git("add", "foreign.txt", cwd=repo)
        _git("commit", "-q", "-m", "foreign file", cwd=repo)
        with open(foreign, "w") as f:
            f.write("local-edit\n")
        _git("stash", "push", "-m", "foreign-work", cwd=repo)

        # Dirty tree now — modify README so precheck will stash it.
        with open(os.path.join(repo, "README.md"), "w") as f:
            f.write("modified\n")

        before_entries = _git("stash", "list", cwd=repo).stdout.strip().splitlines()
        assert len(before_entries) == 1
        assert "foreign-work" in before_entries[0]

        result = _run_precheck(repo, "--command", "false")
        assert result.returncode == 0, result.stderr

        # Our tree edit is restored.
        assert open(os.path.join(repo, "README.md")).read() == "modified\n"
        # The foreign stash is still intact and still the only entry.
        after_entries = _git("stash", "list", cwd=repo).stdout.strip().splitlines()
        assert len(after_entries) == 1
        assert "foreign-work" in after_entries[0]

        payload = _parse_payload(result.stdout)
        assert payload == {
            "pre_existing": True,
            "exit_code": 1,
            "test_command": "false",
            "stashed": True,
        }


# ---------------------------------------------------------------------------
# Command resolution (criterion 248)
# ---------------------------------------------------------------------------


class TestCommandResolution:
    def test_explicit_command_overrides_config(self, tmp_path):
        """When --command is passed, config.test_command is ignored."""
        repo = str(tmp_path / "repo")
        _git_init(repo)

        # Place a tusk/config.json that would otherwise resolve to `false`.
        os.makedirs(os.path.join(repo, "tusk"), exist_ok=True)
        with open(os.path.join(repo, "tusk", "config.json"), "w") as f:
            json.dump({"test_command": "false"}, f)

        result = _run_precheck(repo, "--command", "true")
        assert result.returncode == 0, result.stderr
        payload = _parse_payload(result.stdout)
        assert payload["test_command"] == "true"
        assert payload["exit_code"] == 0

    def test_no_command_available_errors(self, tmp_path):
        """With no --command, no config test_command, and no detectable
        runner, the CLI must error rather than silently succeed."""
        repo = str(tmp_path / "repo")
        _git_init(repo)
        # Empty config — no test_command, no lockfiles.
        os.makedirs(os.path.join(repo, "tusk"), exist_ok=True)
        with open(os.path.join(repo, "tusk", "config.json"), "w") as f:
            json.dump({"test_command": ""}, f)

        result = _run_precheck(repo)
        assert result.returncode == 1
        assert "no test command available" in result.stderr.lower()
