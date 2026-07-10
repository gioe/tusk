"""Unit tests for Claude transcript discovery fallbacks."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_pricing_lib_under_test",
    os.path.join(REPO_ROOT, "bin", "tusk-pricing-lib.py"),
)
pricing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pricing)


def _seed_jsonl(
    home: Path, project_dir: Path, name: str, *, cwd: Path | None = None
) -> Path:
    project_hash = pricing.derive_project_hash(str(project_dir))
    transcript_dir = home / ".claude" / "projects" / project_hash
    transcript_dir.mkdir(parents=True, exist_ok=True)
    jsonl = transcript_dir / f"{name}.jsonl"
    body = {"cwd": str(cwd)} if cwd is not None else {}
    jsonl.write_text(json.dumps(body) + "\n")
    return jsonl


def test_find_transcript_prefers_live_subdirectory_over_stale_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "projects" / "laughtrack"
    subdir = repo / "apps" / "scraper"
    subdir.mkdir(parents=True)
    stale = _seed_jsonl(home, repo, "stale-root")
    live = _seed_jsonl(home, subdir, "live-subdir", cwd=subdir)
    os.utime(stale, (1, 1))
    os.utime(live, (2, 2))

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pricing, "_git_context_dirs", lambda start: [str(repo)])

    assert pricing.find_transcript(str(repo)) == str(live)
    assert pricing.find_all_transcripts_with_fallback(str(repo)) == [str(live), str(stale)]


def test_descendant_hash_rejects_prefix_collision_outside_repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "projects" / "laughtrack"
    sibling = tmp_path / "projects" / "laughtrack-old"
    repo.mkdir(parents=True)
    sibling.mkdir(parents=True)
    collision = _seed_jsonl(home, sibling, "other-repo", cwd=sibling)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pricing, "_git_context_dirs", lambda start: [str(repo)])

    assert pricing.find_transcript(str(repo)) is None
    assert collision.exists()


def test_find_transcript_falls_back_from_task_worktree_to_primary_checkout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    primary = tmp_path / "projects" / "laughtrack"
    worktree = tmp_path / ".tusk" / "worktrees" / "laughtrack" / "TASK-2570-example"
    primary.mkdir(parents=True)
    worktree.mkdir(parents=True)
    expected = _seed_jsonl(home, primary, "orchestrator")

    def fake_run(args, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(returncode=0, stdout=str(worktree) + "\n", stderr="")
        if args == ["git", "rev-parse", "--git-common-dir"]:
            return SimpleNamespace(returncode=0, stdout=str(primary / ".git") + "\n", stderr="")
        raise AssertionError(f"unexpected command: {args!r}")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pricing.subprocess, "run", fake_run)

    assert pricing.find_transcript(str(worktree)) == str(expected)


def test_find_all_transcripts_keeps_normal_hash_dir_behavior(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "projects" / "tusk"
    project.mkdir(parents=True)
    expected = _seed_jsonl(home, project, "normal")

    def fake_run(args, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(returncode=0, stdout=str(project) + "\n", stderr="")
        if args == ["git", "rev-parse", "--git-common-dir"]:
            return SimpleNamespace(returncode=0, stdout=".git\n", stderr="")
        raise AssertionError(f"unexpected command: {args!r}")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pricing.subprocess, "run", fake_run)

    assert pricing.find_all_transcripts_with_fallback(str(project)) == [str(expected)]
