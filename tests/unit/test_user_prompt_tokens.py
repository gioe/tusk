"""Unit tests for user-prompt token counting in tusk-pricing-lib.

Covers:
- _user_prompt_text extracts plain string content
- _user_prompt_text extracts text blocks from a list, skipping tool_result blocks
- estimate_tokens_from_chars uses the chars/4 heuristic
- aggregate_session counts user prompts inside the time window
- aggregate_session skips isMeta entries (local-command-caveat wrappers)
- aggregate_session skips user entries whose content is purely tool_result blocks
- aggregate_session honors the started_at / ended_at filter
- aggregate_session skips slash-command-only entries with empty text
"""

import importlib.util
import json
import os
from datetime import datetime, timezone

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

_spec = importlib.util.spec_from_file_location(
    "tusk_pricing_lib",
    os.path.join(BIN, "tusk-pricing-lib.py"),
)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)


def _write_jsonl(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _user_entry(ts, content, *, is_meta=False):
    entry = {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": content},
    }
    if is_meta:
        entry["isMeta"] = True
    return entry


class TestUserPromptText:

    def test_plain_string_content_returned_as_is(self):
        msg = {"role": "user", "content": "hello world"}
        assert lib._user_prompt_text(msg) == "hello world"

    def test_list_of_text_blocks_concatenated(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "first "},
                {"type": "text", "text": "second"},
            ],
        }
        assert lib._user_prompt_text(msg) == "first second"

    def test_tool_result_blocks_excluded(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "long output"},
                {"type": "text", "text": "what next?"},
            ],
        }
        assert lib._user_prompt_text(msg) == "what next?"

    def test_only_tool_result_returns_empty(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "..."},
            ],
        }
        assert lib._user_prompt_text(msg) == ""

    def test_missing_content_returns_empty(self):
        assert lib._user_prompt_text({"role": "user"}) == ""


class TestEstimateTokensFromChars:

    def test_zero_chars_zero_tokens(self):
        assert lib.estimate_tokens_from_chars(0) == 0

    def test_negative_chars_zero_tokens(self):
        assert lib.estimate_tokens_from_chars(-5) == 0

    def test_chars_divided_by_four(self):
        assert lib.estimate_tokens_from_chars(40) == 10
        assert lib.estimate_tokens_from_chars(7) == 1


class TestAggregateSessionUserPrompts:

    def test_counts_user_prompts_in_window(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            _user_entry("2026-04-20T10:00:00Z", "hello"),
            _user_entry("2026-04-20T10:05:00Z", "what next?"),
        ])
        started = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 4, 20, 11, 0, tzinfo=timezone.utc)

        result = lib.aggregate_session(str(path), started, ended)

        assert result["user_prompt_count"] == 2
        # 5 chars + 10 chars = 15, // 4 → 1 + 2 = 3
        assert result["user_prompt_tokens"] == 1 + 2

    def test_skips_meta_entries(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            _user_entry(
                "2026-04-20T10:00:00Z",
                "<local-command-caveat>...</local-command-caveat>",
                is_meta=True,
            ),
            _user_entry("2026-04-20T10:05:00Z", "real question"),
        ])
        started = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)

        result = lib.aggregate_session(str(path), started, None)

        assert result["user_prompt_count"] == 1
        assert result["user_prompt_tokens"] == len("real question") // 4

    def test_skips_pure_tool_result_entries(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            _user_entry(
                "2026-04-20T10:00:00Z",
                [{"type": "tool_result", "tool_use_id": "x", "content": "X" * 4000}],
            ),
            _user_entry("2026-04-20T10:01:00Z", "actual prompt"),
        ])
        started = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)

        result = lib.aggregate_session(str(path), started, None)

        assert result["user_prompt_count"] == 1
        assert result["user_prompt_tokens"] == len("actual prompt") // 4

    def test_filters_outside_time_window(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            _user_entry("2026-04-20T08:00:00Z", "before window"),
            _user_entry("2026-04-20T10:00:00Z", "in window"),
            _user_entry("2026-04-20T12:00:00Z", "after window"),
        ])
        started = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 4, 20, 11, 0, tzinfo=timezone.utc)

        result = lib.aggregate_session(str(path), started, ended)

        assert result["user_prompt_count"] == 1
        assert result["user_prompt_tokens"] == len("in window") // 4

    def test_empty_text_after_filter_does_not_increment(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_jsonl(path, [
            # Mixed list where every block is a tool_result → no real text → skip.
            _user_entry(
                "2026-04-20T10:00:00Z",
                [
                    {"type": "tool_result", "tool_use_id": "a", "content": "..."},
                    {"type": "tool_result", "tool_use_id": "b", "content": "..."},
                ],
            ),
        ])
        started = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)

        result = lib.aggregate_session(str(path), started, None)

        assert result["user_prompt_count"] == 0
        assert result["user_prompt_tokens"] == 0
