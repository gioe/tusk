"""Helpers for making shell commands portable across linked worktrees."""

from datetime import datetime, timezone
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable


FAILED_TEST_GATE_STATE = "tusk-failed-test-gate.json"
_PYTHON_VENV_RE = re.compile(r"\.venv/bin/python(?:3(?:\.\d+)?)?\b")
_CD_VENV_RE = re.compile(
    r"(?P<prefix>(?:^|&&|\|\||;)\s*cd\s+"
    r"(?P<dir>'[^']+'|\"[^\"]+\"|[^\s;&|]+)\s*&&\s*)"
    r"(?P<python>\.venv/bin/python(?:3(?:\.\d+)?)?)\b"
)
_REPO_RELATIVE_VENV_RE = re.compile(
    r"(?<![\w./-])(?P<path>(?P<dir>[A-Za-z0-9_./-]+)/\.venv/bin/python(?:3(?:\.\d+)?)?)\b"
)
_ROOT_RELATIVE_VENV_RE = re.compile(
    r"(?<![\w./-])(?P<python>\.venv/bin/python(?:3(?:\.\d+)?)?)\b"
)


def _run(args, check=True, cwd=None, **kwargs):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        cwd=cwd,
        **kwargs,
    )


def primary_checkout_root(
    repo_root: str,
    *,
    runner: Callable = _run,
) -> str | None:
    """Return the primary checkout root when ``repo_root`` is a linked worktree."""
    try:
        git_dir = runner(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            check=False,
            cwd=repo_root,
        )
        common_dir = runner(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=False,
            cwd=repo_root,
        )
    except Exception:
        return None

    if git_dir.returncode != 0 or common_dir.returncode != 0:
        return None

    git_dir_path = os.path.realpath(git_dir.stdout.strip())
    common_dir_path = os.path.realpath(common_dir.stdout.strip())
    if not git_dir_path or not common_dir_path or git_dir_path == common_dir_path:
        return None
    if os.path.basename(common_dir_path) != ".git":
        return None
    return os.path.dirname(common_dir_path)


def _failed_test_gate_state_path(
    repo_root: str,
    *,
    runner: Callable = _run,
) -> str:
    """Return a worktree-local Git metadata path for the failed-gate handoff."""
    try:
        result = runner(
            [
                "git", "rev-parse", "--path-format=absolute", "--git-path",
                FAILED_TEST_GATE_STATE,
            ],
            check=False,
            cwd=repo_root,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _head_sha(repo_root: str, *, runner: Callable = _run) -> str:
    try:
        result = runner(
            ["git", "rev-parse", "HEAD"],
            check=False,
            cwd=repo_root,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def clear_failed_test_gate(
    repo_root: str,
    *,
    runner: Callable = _run,
) -> None:
    """Best-effort removal of this worktree's previous failed-gate handoff."""
    path = _failed_test_gate_state_path(repo_root, runner=runner)
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def record_failed_test_gate(
    repo_root: str,
    task_id: int,
    test_command: str,
    exit_code: int,
    *,
    runner: Callable = _run,
) -> None:
    """Atomically record the exact command rejected by this worktree's gate."""
    path = _failed_test_gate_state_path(repo_root, runner=runner)
    head_sha = _head_sha(repo_root, runner=runner)
    if not path or not head_sha or not test_command:
        return
    payload = {
        "version": 1,
        "task_id": int(task_id),
        "head_sha": head_sha,
        "test_command": test_command,
        "exit_code": int(exit_code),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    tmp_path = ""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix=f".{os.path.basename(path)}.",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, sort_keys=True)
            tmp.write("\n")
            tmp_path = tmp.name
        os.replace(tmp_path, path)
    except (OSError, TypeError, ValueError):
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_failed_test_gate_command(
    repo_root: str,
    task_id: int | None,
    *,
    runner: Callable = _run,
) -> str:
    """Return the exact failed command for the current task and HEAD.

    Malformed, foreign-task, and stale-HEAD records fail closed so standalone
    prechecks retain their ordinary path/domain/global command resolution.
    """
    if task_id is None:
        return ""
    path = _failed_test_gate_state_path(repo_root, runner=runner)
    current_head = _head_sha(repo_root, runner=runner)
    if not path or not current_head:
        return ""
    try:
        with open(path, encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if (
        payload.get("version") != 1
        or payload.get("task_id") != task_id
        or payload.get("head_sha") != current_head
    ):
        return ""
    command = payload.get("test_command")
    return command if isinstance(command, str) and command else ""


def _strip_shell_quotes(value: str) -> str:
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    return value


def _candidate(primary_root: str, relative_path: str) -> str:
    return os.path.normpath(os.path.join(primary_root, relative_path))


def rewrite_linked_worktree_venv_command(
    command: str,
    repo_root: str,
    *,
    runner: Callable = _run,
    exists: Callable[[str], bool] = os.path.exists,
) -> tuple[str, bool]:
    """Rewrite relative ``.venv/bin/python*`` references for linked worktrees.

    The command still runs with ``cwd`` set to the task worktree. Only Python
    interpreter paths are rewritten to point at the primary checkout's ignored
    virtualenv, so tests execute against the worktree source while reusing the
    venv that exists outside the linked checkout.
    """
    primary_root = primary_checkout_root(repo_root, runner=runner)
    if primary_root is None or not _PYTHON_VENV_RE.search(command):
        return command, False

    rewritten = command
    did_rewrite = False

    def replace_cd(match: re.Match) -> str:
        nonlocal did_rewrite
        cd_dir = _strip_shell_quotes(match.group("dir"))
        python = match.group("python")
        candidate = _candidate(primary_root, os.path.join(cd_dir, python))
        if not exists(candidate):
            return match.group(0)
        did_rewrite = True
        return f"{match.group('prefix')}{shlex.quote(candidate)}"

    rewritten = _CD_VENV_RE.sub(replace_cd, rewritten)

    def replace_repo_relative(match: re.Match) -> str:
        nonlocal did_rewrite
        candidate = _candidate(primary_root, match.group("path"))
        if not exists(candidate):
            return match.group(0)
        did_rewrite = True
        return shlex.quote(candidate)

    rewritten = _REPO_RELATIVE_VENV_RE.sub(replace_repo_relative, rewritten)

    def replace_root_relative(match: re.Match) -> str:
        nonlocal did_rewrite
        candidate = _candidate(primary_root, match.group("python"))
        if not exists(candidate):
            return match.group(0)
        did_rewrite = True
        return shlex.quote(candidate)

    rewritten = _ROOT_RELATIVE_VENV_RE.sub(replace_root_relative, rewritten)
    return rewritten, did_rewrite
