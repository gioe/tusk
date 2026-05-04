"""Unit tests for tusk-review.py cmd_begin.

cmd_begin bundles the diff-range computation and the code_reviews row creation
into one call. It must:

1. Compute the diff range against the task's branch (delegating to
   tusk-review-diff-range.compute_range)
2. Insert one pending code_reviews row with the captured summary baked in
3. Print combined JSON on stdout: review_id, task_id, reviewer, range,
   diff_lines, recovered_from_task_commits — with `summary` deliberately
   omitted so callers never have to pipe raw diff output through jq

The diff-range computation is monkeypatched to keep these tests pure unit
tests (no real git repo). End-to-end coverage of the diff-range path lives in
test_review_diff_range.py.
"""

import argparse
import importlib.util
import json
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "tusk_review",
    os.path.join(REPO_ROOT, "bin", "tusk-review.py"),
)
review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review)


def _make_db(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT
        );
        CREATE TABLE code_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            reviewer TEXT,
            status TEXT DEFAULT 'pending',
            review_pass INTEGER DEFAULT 1,
            diff_summary TEXT,
            agent_name TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO tasks (id, summary) VALUES (1, 'sample task');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _make_config(tmp_path, reviewer=None):
    cfg = {
        "review": {"mode": "ai_only", "max_passes": 2},
        "review_categories": ["must_fix", "suggest"],
        "review_severities": ["critical", "major", "minor"],
    }
    if reviewer is not None:
        cfg["review"]["reviewer"] = reviewer
    config_path = str(tmp_path / "config.json")
    with open(config_path, "w") as f:
        json.dump(cfg, f)
    return config_path


def _args(task_id=1, reviewer=None, pass_num=1, agent=None):
    return argparse.Namespace(
        task_id=task_id,
        reviewer=reviewer,
        pass_num=pass_num,
        agent=agent,
    )


def _stub_diff_range(payload):
    """Patch tusk_loader.load to return a fake diff-range module."""

    class _FakeMod:
        def compute_range(self, task_id, repo_root):
            if isinstance(payload, BaseException):
                raise payload
            return payload

    fake = _FakeMod()
    return fake


class TestCmdBeginHappyPath:
    def test_inserts_review_and_prints_combined_json(self, tmp_path, capsys, monkeypatch):
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, {"name": "general", "description": "..."})

        diff_payload = {
            "range": "main...HEAD",
            "diff_lines": 42,
            "summary": "diff --git a/foo b/foo\n+new line\n",
            "recovered_from_task_commits": False,
        }
        fake_mod = _stub_diff_range(diff_payload)
        monkeypatch.setattr(
            review.tusk_loader,
            "load",
            lambda name: fake_mod if name == "tusk-review-diff-range" else review.tusk_loader.load(name),
        )

        rc = review.cmd_begin(_args(), db_path, config_path)
        assert rc == 0

        out = json.loads(capsys.readouterr().out)
        assert out["review_id"] >= 1
        assert out["task_id"] == 1
        assert out["reviewer"] == "general"
        assert out["range"] == "main...HEAD"
        assert out["diff_lines"] == 42
        assert out["recovered_from_task_commits"] is False
        # Summary is deliberately NOT exposed on stdout — that's the whole point
        # of bundling. Callers never touch raw diff output in shell.
        assert "summary" not in out

        # Verify the diff summary was persisted on the DB row, even though it
        # was suppressed from stdout.
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT reviewer, status, review_pass, diff_summary FROM code_reviews WHERE id = ?",
            (out["review_id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "general"
        assert row[1] == "pending"
        assert row[2] == 1
        assert row[3] == diff_payload["summary"]


class TestCmdBeginSupersedesPriorPending:
    def test_prior_pending_review_marked_superseded(self, tmp_path, capsys, monkeypatch):
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, {"name": "general", "description": "..."})

        # Seed a prior pending review on the same task.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO code_reviews (task_id, reviewer, status, review_pass, diff_summary)"
            " VALUES (1, 'general', 'pending', 1, 'old')"
        )
        conn.commit()
        prior_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        fake_mod = _stub_diff_range({
            "range": "main...HEAD",
            "diff_lines": 10,
            "summary": "x",
            "recovered_from_task_commits": False,
        })
        monkeypatch.setattr(
            review.tusk_loader,
            "load",
            lambda name: fake_mod if name == "tusk-review-diff-range" else review.tusk_loader.load(name),
        )

        rc = review.cmd_begin(_args(pass_num=2), db_path, config_path)
        assert rc == 0

        conn = sqlite3.connect(db_path)
        prior_status = conn.execute(
            "SELECT status FROM code_reviews WHERE id = ?", (prior_id,)
        ).fetchone()[0]
        new_count = conn.execute(
            "SELECT COUNT(*) FROM code_reviews WHERE task_id = 1 AND status = 'pending'"
        ).fetchone()[0]
        conn.close()

        assert prior_status == "superseded"
        assert new_count == 1


class TestCmdBeginEmptyDiffPropagates:
    def test_empty_diff_returns_1_and_writes_stderr(self, tmp_path, capsys, monkeypatch):
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path)

        fake_mod = _stub_diff_range(SystemExit("No changes found compared to the base branch."))
        monkeypatch.setattr(
            review.tusk_loader,
            "load",
            lambda name: fake_mod if name == "tusk-review-diff-range" else review.tusk_loader.load(name),
        )

        rc = review.cmd_begin(_args(), db_path, config_path)
        assert rc == 1

        captured = capsys.readouterr()
        assert "No changes found compared to the base branch." in captured.err
        assert captured.out == ""

        # Verify no code_reviews row was inserted on the failure path.
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM code_reviews").fetchone()[0]
        conn.close()
        assert count == 0


class TestCmdBeginTaskNotFound:
    def test_unknown_task_returns_2(self, tmp_path, capsys, monkeypatch):
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path)

        fake_mod = _stub_diff_range({
            "range": "main...HEAD",
            "diff_lines": 1,
            "summary": "x",
            "recovered_from_task_commits": False,
        })
        monkeypatch.setattr(
            review.tusk_loader,
            "load",
            lambda name: fake_mod if name == "tusk-review-diff-range" else review.tusk_loader.load(name),
        )

        rc = review.cmd_begin(_args(task_id=99), db_path, config_path)
        assert rc == 2

        captured = capsys.readouterr()
        assert "Task 99 not found" in captured.err


class TestCmdBeginReviewerOverride:
    def test_cli_reviewer_flag_overrides_config(self, tmp_path, capsys, monkeypatch):
        db_path = _make_db(tmp_path)
        config_path = _make_config(tmp_path, {"name": "general", "description": "..."})

        fake_mod = _stub_diff_range({
            "range": "main...HEAD",
            "diff_lines": 5,
            "summary": "x",
            "recovered_from_task_commits": False,
        })
        monkeypatch.setattr(
            review.tusk_loader,
            "load",
            lambda name: fake_mod if name == "tusk-review-diff-range" else review.tusk_loader.load(name),
        )

        rc = review.cmd_begin(_args(reviewer="security"), db_path, config_path)
        assert rc == 0

        out = json.loads(capsys.readouterr().out)
        assert out["reviewer"] == "security"
