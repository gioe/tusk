"""Unit tests for iter_tool_errors / _extract_error_text in tusk-pricing-lib.

Covers transcript parsing for tool failures across every payload shape
Claude Code emits: `<tool_use_error>` wrapper (Edit/Read guards), bare string
with `Exit code N` prefix (Bash failures), and list-of-text-blocks
(long assistant replies wrapped in a content list).
"""

import importlib.util
import json
import os
from datetime import datetime, timezone

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


def _assistant(ts, tool_use_id, tool_name):
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": f"req-{tool_use_id}",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name}],
        },
    }


def _user_error(ts, tool_use_id, content):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": content,
                }
            ]
        },
    }


def _user_ok(ts, tool_use_id, text):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
            ]
        },
    }


# ── _extract_error_text ───────────────────────────────────────────────


class TestExtractErrorText:
    def test_unwraps_tool_use_error_tag(self):
        raw = "<tool_use_error>File has not been read yet.</tool_use_error>"
        assert lib._extract_error_text(raw) == "File has not been read yet."

    def test_collapses_interior_whitespace(self):
        raw = "Exit code 1\n  stderr line\n\tmore"
        assert lib._extract_error_text(raw) == "Exit code 1 stderr line more"

    def test_joins_list_of_text_blocks(self):
        raw = [
            {"type": "text", "text": "first chunk"},
            {"type": "text", "text": "second chunk"},
        ]
        assert lib._extract_error_text(raw) == "first chunk second chunk"

    def test_handles_none_and_unknown_shape(self):
        assert lib._extract_error_text(None) == ""
        assert lib._extract_error_text(42) == ""
        assert lib._extract_error_text({"not": "a list"}) == ""

    def test_leaves_partial_wrapper_untouched(self):
        # Only strips the wrapper when BOTH ends match — a stray opening tag
        # in a Bash error message is real content and must not be eaten.
        raw = "Error: saw <tool_use_error> in log output"
        assert lib._extract_error_text(raw) == "Error: saw <tool_use_error> in log output"


# ── iter_tool_errors ──────────────────────────────────────────────────


class TestIterToolErrors:
    def test_yields_only_is_error_entries(self, tmp_path):
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            _assistant("2026-04-19T12:00:00Z", "tu_1", "Bash"),
            _user_ok("2026-04-19T12:00:01Z", "tu_1", "success output"),
            _assistant("2026-04-19T12:00:02Z", "tu_2", "Edit"),
            _user_error("2026-04-19T12:00:03Z", "tu_2",
                        "<tool_use_error>File has not been read yet.</tool_use_error>"),
        ])
        start = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        out = list(lib.iter_tool_errors(str(path), start, None))
        assert len(out) == 1
        assert out[0]["tool_name"] == "Edit"
        assert out[0]["error_text"] == "File has not been read yet."

    def test_resolves_tool_name_across_split_messages(self, tmp_path):
        # Matching assistant tool_use precedes the user error a few lines later.
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            _assistant("2026-04-19T12:00:00Z", "tu_bash_a", "Bash"),
            _user_ok("2026-04-19T12:00:01Z", "tu_bash_a", "ok"),
            _assistant("2026-04-19T12:00:02Z", "tu_read_a", "Read"),
            _user_error("2026-04-19T12:00:03Z", "tu_read_a",
                        "<tool_use_error>File content exceeds maximum.</tool_use_error>"),
            _assistant("2026-04-19T12:00:04Z", "tu_bash_b", "Bash"),
            _user_error("2026-04-19T12:00:05Z", "tu_bash_b", "Exit code 2\nsomething failed"),
        ])
        out = list(lib.iter_tool_errors(str(path), datetime(2026, 4, 19, tzinfo=timezone.utc), None))
        assert [r["tool_name"] for r in out] == ["Read", "Bash"]
        assert out[1]["error_text"] == "Exit code 2 something failed"

    def test_unknown_tool_when_no_matching_assistant_message(self, tmp_path):
        # Error arrives for a tool_use_id the iterator never saw — happens when
        # a session was split across transcripts (compaction/crash).
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            _user_error("2026-04-19T12:00:00Z", "tu_orphan",
                        "<tool_use_error>Cancelled</tool_use_error>"),
        ])
        out = list(lib.iter_tool_errors(str(path), datetime(2026, 4, 19, tzinfo=timezone.utc), None))
        assert len(out) == 1
        assert out[0]["tool_name"] == "(unknown)"

    def test_respects_time_window(self, tmp_path):
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            _assistant("2026-04-19T10:00:00Z", "tu_early", "Bash"),
            _user_error("2026-04-19T10:00:01Z", "tu_early", "Exit code 1"),
            _assistant("2026-04-19T12:00:00Z", "tu_mid", "Edit"),
            _user_error("2026-04-19T12:00:01Z", "tu_mid",
                        "<tool_use_error>in window</tool_use_error>"),
            _assistant("2026-04-19T14:00:00Z", "tu_late", "Read"),
            _user_error("2026-04-19T14:00:01Z", "tu_late",
                        "<tool_use_error>after window</tool_use_error>"),
        ])
        start = datetime(2026, 4, 19, 11, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc)
        out = list(lib.iter_tool_errors(str(path), start, end))
        assert [r["tool_name"] for r in out] == ["Edit"]
        assert out[0]["error_text"] == "in window"

    def test_skips_non_error_tool_results(self, tmp_path):
        # A tool_result without is_error (or with is_error: False) must not leak.
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            _assistant("2026-04-19T12:00:00Z", "tu_1", "Bash"),
            {
                "type": "user",
                "timestamp": "2026-04-19T12:00:01Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "is_error": False,
                            "content": "normal output",
                        }
                    ]
                },
            },
        ])
        out = list(lib.iter_tool_errors(str(path), datetime(2026, 4, 19, tzinfo=timezone.utc), None))
        assert out == []

    def test_ignores_unparseable_lines(self, tmp_path):
        path = tmp_path / "t.jsonl"
        with open(path, "w") as f:
            f.write("not json at all\n")
            f.write(json.dumps(_assistant("2026-04-19T12:00:00Z", "tu_1", "Bash")) + "\n")
            f.write("\n")  # blank
            f.write(json.dumps(_user_error(
                "2026-04-19T12:00:01Z", "tu_1", "Exit code 99"
            )) + "\n")
        out = list(lib.iter_tool_errors(str(path), datetime(2026, 4, 19, tzinfo=timezone.utc), None))
        assert len(out) == 1
        assert out[0]["tool_name"] == "Bash"
