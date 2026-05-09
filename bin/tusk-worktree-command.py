"""Helpers for making shell commands portable across linked worktrees."""

import os
import re
import shlex
import subprocess
from collections.abc import Callable


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
