"""Regression tests for transient GitHub HTTP 429 retries (issue #1198)."""

import importlib.util
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "tusk_github_under_test", REPO_ROOT / "bin" / "tusk_github.py"
)
github = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(github)


def _response(payload: bytes):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = payload
    return response


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://example.invalid/VERSION", code, "error", {}, None)


def test_http_429_retries_until_success(monkeypatch, capsys):
    urlopen = MagicMock(side_effect=[_http_error(429), _response(b"1221\n")])
    sleep = MagicMock()
    monkeypatch.setattr(github, "urlopen", urlopen)
    monkeypatch.setattr(github.time, "sleep", sleep)

    assert github.get_remote_version("v1221") == 1221
    assert urlopen.call_count == 2
    sleep.assert_called_once_with(github.VERSION_429_RETRY_DELAYS[0])
    assert "HTTP 429" in capsys.readouterr().err


def test_http_429_exhaustion_reports_attempt_count(monkeypatch):
    attempts = len(github.VERSION_429_RETRY_DELAYS) + 1
    urlopen = MagicMock(side_effect=[_http_error(429) for _ in range(attempts)])
    sleep = MagicMock()
    monkeypatch.setattr(github, "urlopen", urlopen)
    monkeypatch.setattr(github.time, "sleep", sleep)

    with pytest.raises(SystemExit, match=f"HTTP 429 .* after {attempts} attempts"):
        github.get_remote_version("v1221")

    assert urlopen.call_count == attempts
    assert [call.args[0] for call in sleep.call_args_list] == list(
        github.VERSION_429_RETRY_DELAYS
    )


def test_non_429_http_error_fails_without_retry(monkeypatch):
    urlopen = MagicMock(side_effect=_http_error(404))
    sleep = MagicMock()
    monkeypatch.setattr(github, "urlopen", urlopen)
    monkeypatch.setattr(github.time, "sleep", sleep)

    with pytest.raises(SystemExit, match="HTTP 404 fetching"):
        github.fetch_bytes("https://example.invalid/VERSION")

    urlopen.assert_called_once()
    sleep.assert_not_called()


def test_plain_fetch_keeps_429_as_immediate_failure(monkeypatch):
    urlopen = MagicMock(side_effect=_http_error(429))
    sleep = MagicMock()
    monkeypatch.setattr(github, "urlopen", urlopen)
    monkeypatch.setattr(github.time, "sleep", sleep)

    with pytest.raises(SystemExit, match="HTTP 429 fetching"):
        github.fetch_bytes("https://example.invalid/tarball")

    urlopen.assert_called_once()
    sleep.assert_not_called()
