"""Unit tests for Codex transcript support in tusk-pricing-lib."""

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
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _session_meta(ts="2026-04-20T22:50:23.667Z"):
    return {
        "timestamp": "2026-04-20T22:50:48.560Z",
        "type": "session_meta",
        "payload": {
            "id": "thread-1",
            "timestamp": ts,
            "cwd": "/tmp/project",
            "originator": "Codex Desktop",
            "model_provider": "openai",
        },
    }


def _tool_call(ts, name, call_id):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
        },
    }


def _web_search_call(ts, call_id):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "web_search_call",
            "call_id": call_id,
        },
    }


def _token_count(ts, *, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, context_window=258400):
    total_tokens = input_tokens + output_tokens
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_output_tokens,
                    "total_tokens": total_tokens,
                },
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_output_tokens,
                    "total_tokens": total_tokens,
                },
                "model_context_window": context_window,
            },
        },
    }


def _tool_end(ts, payload_type, call_id, *, exit_code=0, stderr="", status="completed"):
    payload = {
        "type": payload_type,
        "call_id": call_id,
        "status": status,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if stderr:
        payload["stderr"] = stderr
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": payload,
    }


class TestCodexAggregateSession:
    def test_aggregate_session_maps_codex_token_count_events(self, tmp_path, monkeypatch):
        path = tmp_path / "codex.jsonl"
        _write_jsonl(path, [
            _session_meta(),
            _token_count(
                "2026-04-20T22:50:55.004Z",
                input_tokens=1000,
                cached_input_tokens=200,
                output_tokens=30,
                reasoning_output_tokens=10,
            ),
            _token_count(
                "2026-04-20T22:51:02.950Z",
                input_tokens=1500,
                cached_input_tokens=500,
                output_tokens=50,
                reasoning_output_tokens=20,
            ),
            _token_count(
                "2026-04-20T22:52:02.950Z",
                input_tokens=9999,
                cached_input_tokens=9999,
                output_tokens=999,
                reasoning_output_tokens=999,
            ),
        ])
        monkeypatch.setattr(
            lib,
            "_lookup_codex_thread_meta",
            lambda transcript_path: {"model": "gpt-5.4"},
        )

        out = lib.aggregate_session(
            str(path),
            datetime(2026, 4, 20, 22, 50, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 20, 22, 51, 30, tzinfo=timezone.utc),
        )

        assert out["model"] == "gpt-5.4"
        assert out["request_count"] == 2
        assert out["input_tokens"] == 2500
        assert out["cache_read_input_tokens"] == 700
        assert out["output_tokens"] == 110, "reasoning output must be folded into tokens_out"
        assert out["first_context_tokens"] == 1200
        assert out["peak_context_tokens"] == 2000
        assert out["last_context_tokens"] == 2000
        assert out["context_window"] == 258400


class TestCodexToolCallCosts:
    def test_iter_tool_call_costs_splits_turn_usage_across_pending_calls(self, tmp_path, monkeypatch):
        path = tmp_path / "codex-tools.jsonl"
        _write_jsonl(path, [
            _session_meta(),
            _tool_call("2026-04-20T22:50:50.000Z", "exec_command", "call_a"),
            _web_search_call("2026-04-20T22:50:50.100Z", "call_b"),
            _token_count(
                "2026-04-20T22:50:55.004Z",
                input_tokens=1000,
                cached_input_tokens=200,
                output_tokens=30,
                reasoning_output_tokens=10,
            ),
        ])
        monkeypatch.setattr(
            lib,
            "_lookup_codex_thread_meta",
            lambda transcript_path: {"model": "gpt-5.4"},
        )
        monkeypatch.setattr(
            lib,
            "PRICING",
            {
                "gpt-5.4": {
                    "input": 2.5,
                    "cache_write_5m": 0.0,
                    "cache_write_1h": 0.0,
                    "cache_read": 0.25,
                    "output": 15.0,
                }
            },
        )

        out = list(
            lib.iter_tool_call_costs(
                str(path),
                datetime(2026, 4, 20, 22, 50, 0, tzinfo=timezone.utc),
                None,
            )
        )

        assert [row["tool_name"] for row in out] == ["exec_command", "web_search"]
        assert [row["marginal_input_tokens"] for row in out] == [500, 500]
        assert [row["output_tokens"] for row in out] == [20, 20]

        expected_total_cost = (
            1000 / 1_000_000 * 2.5
            + 200 / 1_000_000 * 0.25
            + 40 / 1_000_000 * 15.0
        )
        assert out[0]["cost"] == pytest.approx(expected_total_cost / 2, rel=1e-9)
        assert out[1]["cost"] == pytest.approx(expected_total_cost / 2, rel=1e-9)


class TestCodexToolErrors:
    def test_iter_tool_errors_reads_failed_end_events(self, tmp_path):
        path = tmp_path / "codex-errors.jsonl"
        _write_jsonl(path, [
            _session_meta(),
            _tool_call("2026-04-20T22:50:50.000Z", "exec_command", "call_a"),
            _tool_end(
                "2026-04-20T22:50:50.500Z",
                "exec_command_end",
                "call_a",
                exit_code=2,
                stderr="Exit code 2\nboom",
                status="failed",
            ),
            _web_search_call("2026-04-20T22:50:51.000Z", "call_b"),
            _tool_end(
                "2026-04-20T22:50:51.500Z",
                "web_search_end",
                "call_b",
                exit_code=0,
                status="completed",
            ),
        ])

        out = list(
            lib.iter_tool_errors(
                str(path),
                datetime(2026, 4, 20, 22, 50, 0, tzinfo=timezone.utc),
                None,
            )
        )

        assert len(out) == 1
        assert out[0]["tool_name"] == "exec_command"
        assert out[0]["error_text"] == "Exit code 2 boom"
