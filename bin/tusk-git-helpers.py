"""Shared git remote helpers for tusk scripts.

Extracted from tusk-branch.py and tusk-merge.py to prevent drift of the
unreachable-remote detection patterns. The `_has_remote` wrapper itself is
left in each caller so that it uses each script's module-local ``run`` (which
tests patch to stub subprocess calls).

Loaded via tusk_loader:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _git_helpers = tusk_loader.load("tusk-git-helpers")
    _is_remote_unreachable = _git_helpers._is_remote_unreachable
    _UNREACHABLE_REMOTE_PATTERNS = _git_helpers._UNREACHABLE_REMOTE_PATTERNS
    _UNREACHABLE_REMOTE_REGEX = _git_helpers._UNREACHABLE_REMOTE_REGEX
"""

import re


_UNREACHABLE_REMOTE_PATTERNS = (
    "unable to access",
    "could not resolve host",
    "could not read from remote repository",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "repository not found",
    "does not appear to be a git repository",
    "temporary failure in name resolution",
    "name or service not known",
    "no route to host",
)

# git sometimes inlines the failing URL: `fatal: repository 'https://…' not found`.
_UNREACHABLE_REMOTE_REGEX = re.compile(r"repository '[^']*' not found", re.IGNORECASE)


def _is_remote_unreachable(stderr: str) -> bool:
    """Return True if *stderr* indicates the remote is unreachable rather than
    a local merge problem. Used to distinguish network/DNS/404 failures (where
    we can safely fall back to local state) from divergent-history or merge
    conflicts (where we must hard-fail)."""
    lower = stderr.lower()
    if any(pat in lower for pat in _UNREACHABLE_REMOTE_PATTERNS):
        return True
    return bool(_UNREACHABLE_REMOTE_REGEX.search(stderr))
