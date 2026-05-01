"""Unit tests for tusk-review-agent-cost.py.

The helper discovers Claude Code subagent JSONL transcripts that were
created or modified during a /review-commits agent-path review and sums
their cost. The orchestrator's own JSONL is excluded.
"""

import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_review_agent_cost",
    os.path.join(REPO_ROOT, "bin", "tusk-review-agent-cost.py"),
)
agent_cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_cost)


class _StubLib:
    """Stand in for tusk-pricing-lib so tests don't read real transcripts."""

    def __init__(self, project_hash: str, totals_by_path: dict):
        self._hash = project_hash
        self.totals_by_path = totals_by_path
        self.aggregate_calls: list[tuple[str, datetime]] = []

    def derive_project_hash(self, project_dir: str) -> str:
        return self._hash

    def aggregate_session(self, jsonl_path: str, started_at: datetime, _end):
        self.aggregate_calls.append((jsonl_path, started_at))
        return self.totals_by_path.get(jsonl_path, {"request_count": 0})

    def compute_cost(self, totals: dict) -> float:
        return totals.get("cost_dollars", 0.0)

    def compute_tokens_in(self, totals: dict) -> int:
        return totals.get("tokens_in", 0)


def _seed_jsonl(claude_dir: Path, name: str, mtime: float, body: str = "{}\n") -> Path:
    """Create a JSONL file at the given path with a controlled mtime."""
    jsonl = claude_dir / f"{name}.jsonl"
    jsonl.write_text(body)
    os.utime(jsonl, (mtime, mtime))
    return jsonl


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() so tests don't touch the user's real ~/.claude."""
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _claude_dir_for(home: Path, project_hash: str) -> Path:
    d = home / ".claude" / "projects" / project_hash
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestCandidateFiltering:
    def test_excludes_orchestrator_jsonl_and_old_files(self, fake_home, monkeypatch):
        project_hash = "fake_hash_123"
        claude_dir = _claude_dir_for(fake_home, project_hash)
        spawn_time = time.time() - 100  # spawn was 100s ago

        # Orchestrator's JSONL: continuously updated, mtime > spawn.
        orch = _seed_jsonl(claude_dir, "orchestrator", mtime=spawn_time + 50)
        # Agent JSONL: created during the window.
        agent = _seed_jsonl(claude_dir, "agent_one", mtime=spawn_time + 30)
        # Stale JSONL: older than spawn — must be excluded.
        _seed_jsonl(claude_dir, "stale", mtime=spawn_time - 200)

        stub = _StubLib(
            project_hash,
            {
                str(agent): {
                    "request_count": 5,
                    "cost_dollars": 0.42,
                    "tokens_in": 1000,
                    "output_tokens": 200,
                },
            },
        )
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        result = agent_cost.aggregate_agent_cost(
            since_epoch=spawn_time,
            exclude_jsonl=str(orch),
            project_dir="/fake/project",
        )

        assert result["cost_dollars"] == 0.42
        assert result["tokens_in"] == 1000
        assert result["tokens_out"] == 200
        assert result["request_count"] == 5
        assert result["transcripts"] == [str(agent)]

    def test_aggregates_multiple_agent_jsonls(self, fake_home, monkeypatch):
        project_hash = "fake_hash_multi"
        claude_dir = _claude_dir_for(fake_home, project_hash)
        spawn_time = time.time() - 50

        orch = _seed_jsonl(claude_dir, "orch", mtime=spawn_time + 10)
        a1 = _seed_jsonl(claude_dir, "agent_a", mtime=spawn_time + 20)
        a2 = _seed_jsonl(claude_dir, "agent_b", mtime=spawn_time + 25)

        stub = _StubLib(
            project_hash,
            {
                str(a1): {"request_count": 2, "cost_dollars": 0.10, "tokens_in": 100, "output_tokens": 10},
                str(a2): {"request_count": 3, "cost_dollars": 0.20, "tokens_in": 200, "output_tokens": 20},
            },
        )
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        result = agent_cost.aggregate_agent_cost(
            since_epoch=spawn_time,
            exclude_jsonl=str(orch),
            project_dir="/fake/project",
        )

        assert result["request_count"] == 5
        assert result["cost_dollars"] == pytest.approx(0.30)
        assert result["tokens_in"] == 300
        assert result["tokens_out"] == 30
        assert sorted(result["transcripts"]) == sorted([str(a1), str(a2)])

    def test_skips_agent_jsonls_with_zero_requests(self, fake_home, monkeypatch):
        project_hash = "fake_hash_empty"
        claude_dir = _claude_dir_for(fake_home, project_hash)
        spawn_time = time.time() - 30

        empty_agent = _seed_jsonl(claude_dir, "empty_agent", mtime=spawn_time + 5)
        real_agent = _seed_jsonl(claude_dir, "real_agent", mtime=spawn_time + 10)

        stub = _StubLib(
            project_hash,
            {
                str(empty_agent): {"request_count": 0},
                str(real_agent): {"request_count": 1, "cost_dollars": 0.05, "tokens_in": 50, "output_tokens": 5},
            },
        )
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        result = agent_cost.aggregate_agent_cost(
            since_epoch=spawn_time,
            exclude_jsonl=None,
            project_dir="/fake/project",
        )

        assert result["transcripts"] == [str(real_agent)]
        assert result["request_count"] == 1


class TestEmptyResults:
    def test_no_candidates_returns_empty_aggregate(self, fake_home, monkeypatch):
        project_hash = "fake_hash_none"
        claude_dir = _claude_dir_for(fake_home, project_hash)
        spawn_time = time.time()

        # Only a stale JSONL exists.
        _seed_jsonl(claude_dir, "old", mtime=spawn_time - 1000)

        stub = _StubLib(project_hash, {})
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        result = agent_cost.aggregate_agent_cost(
            since_epoch=spawn_time,
            exclude_jsonl=None,
            project_dir="/fake/project",
        )

        assert result["request_count"] == 0
        assert result["cost_dollars"] == 0.0
        assert result["transcripts"] == []

    def test_missing_project_dir_returns_empty(self, fake_home, monkeypatch):
        # No claude_dir created — project hash dir simply doesn't exist.
        stub = _StubLib("nonexistent_hash", {})
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        result = agent_cost.aggregate_agent_cost(
            since_epoch=time.time(),
            exclude_jsonl=None,
            project_dir="/fake/project",
        )

        assert result["request_count"] == 0
        assert result["transcripts"] == []


class TestMainEntrypoint:
    def test_main_exits_0_when_aggregation_succeeds(self, fake_home, monkeypatch, capsys):
        project_hash = "fake_main_hash"
        claude_dir = _claude_dir_for(fake_home, project_hash)
        spawn_time = time.time() - 10
        agent = _seed_jsonl(claude_dir, "agent", mtime=spawn_time + 1)

        stub = _StubLib(
            project_hash,
            {str(agent): {"request_count": 1, "cost_dollars": 0.01, "tokens_in": 10, "output_tokens": 1}},
        )
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        rc = agent_cost.main([
            "--since", str(spawn_time),
            "--project-dir", str(fake_home),  # only used for hash derivation (stubbed)
        ])
        assert rc == 0

        out = json.loads(capsys.readouterr().out)
        assert out["request_count"] == 1
        assert out["cost_dollars"] == 0.01

    def test_main_exits_1_when_no_candidates(self, fake_home, monkeypatch, capsys):
        project_hash = "fake_empty_main"
        _claude_dir_for(fake_home, project_hash)  # exists but empty

        stub = _StubLib(project_hash, {})
        monkeypatch.setattr(agent_cost, "_load_pricing_lib", lambda: stub)

        rc = agent_cost.main([
            "--since", str(time.time()),
            "--project-dir", str(fake_home),
        ])
        assert rc == 1

        out = json.loads(capsys.readouterr().out)
        assert out["request_count"] == 0
        assert out["transcripts"] == []

    def test_main_exits_2_when_project_dir_missing(self, capsys):
        rc = agent_cost.main([
            "--since", str(time.time()),
            "--project-dir", "/definitely/does/not/exist",
        ])
        assert rc == 2
        assert "project dir not found" in capsys.readouterr().err
