#!/usr/bin/env python3
"""Shared GitHub-fetch helpers for tusk's distributed scripts.

`tusk-upgrade.py` and `tusk-reconcile-skills.py` both need to talk to the
GitHub release API and download the tarball. Hyphenated module names aren't
importable, so the helpers live in an underscored module that both scripts
import — same pattern as `tusk_skill_filter.py`.
"""

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GITHUB_REPO = "gioe/tusk"
API_TIMEOUT = 15   # seconds for GitHub API calls
DL_TIMEOUT = 60    # seconds for tarball download


def _ssl_context() -> ssl.SSLContext:
    """Return an SSL context with system/certifi certs, falling back to default."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(capath="/etc/ssl/certs")
    except (FileNotFoundError, ssl.SSLError):
        pass
    return ctx


def fetch_bytes(url: str, timeout: int = API_TIMEOUT) -> bytes:
    req = Request(url, headers={"User-Agent": "tusk-upgrade"})
    try:
        with urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except HTTPError as e:
        raise SystemExit(f"Error: HTTP {e.code} fetching {url}") from e
    except URLError as e:
        raise SystemExit(f"Error: Could not reach {url}: {e.reason}") from e


def get_latest_tag() -> str:
    data = fetch_bytes(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    )
    try:
        return json.loads(data)["tag_name"]
    except (KeyError, json.JSONDecodeError) as e:
        raise SystemExit(f"Error: Could not parse latest release from GitHub: {e}") from e


def get_remote_version(tag: str) -> int:
    raw = fetch_bytes(
        f"https://raw.githubusercontent.com/{GITHUB_REPO}/refs/tags/{tag}/VERSION"
    )
    try:
        return int(raw.strip())
    except ValueError as e:
        raise SystemExit(f"Error: Could not parse remote VERSION: {e}") from e


def tarball_url(tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.tar.gz"
