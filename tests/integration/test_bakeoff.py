"""Integration test for tusk bakeoff end-to-end (2-model attempt).

Covers:
- --models with fewer than 2 identifiers fails with a usage error.
- A 2-model bakeoff clones two shadow rows with a shared bakeoff_id and
  bakeoff_shadow=1, copying every acceptance criterion from the source task.
- The bakeoff command creates one git worktree per model on a deterministic
  branch name that encodes the bakeoff_id and shadow_id.
- The final markdown report contains one column per model plus a pairwise
  diff section — criterion 551's two required assertions.

The test monkeypatches the worktree creation, agent spawn, and pairwise diff
stat helpers so the test doesn't need a real git repo or a Claude subprocess.
Model identifiers are stubbed ("stub-a", "stub-b"); the stubs record the
arguments they received so the test can verify per-model dispatch.
"""

import importlib.util
import io
import os
import sqlite3
import subprocess
from contextlib import redirect_stderr, redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(REPO_ROOT, "bin")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(SCRIPT_DIR, f"{name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tusk_bakeoff = _load("tusk-bakeoff")


def _insert_source_task(db_path: str) -> tuple[int, list[int]]:
    """Insert a realistic source task with two acceptance criteria."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score) "
            "VALUES ('source task', 'bakeoff source description', 'To Do', 'feature', "
            "'Medium', 'S', 50)"
        )
        task_id = cur.lastrowid
        crit_ids = []
        for text in ("criterion A", "criterion B"):
            cur = conn.execute(
                "INSERT INTO acceptance_criteria (task_id, criterion, source) "
                "VALUES (?, ?, 'original')",
                (task_id, text),
            )
            crit_ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return task_id, crit_ids


class TestBakeoffModelsParsing:

    def test_rejects_single_model(self, db_path, config_path):
        """--models foo with only one identifier must fail with exit 1."""
        task_id, _ = _insert_source_task(db_path)

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main(
                [str(db_path), str(config_path), str(task_id), "--models", "onlyone"]
            )

        assert exit_code == 1
        assert "at least 2" in stderr_buf.getvalue()

    def test_rejects_missing_task(self, db_path, config_path):
        """Referencing a non-existent task id must fail with exit 1 (not a crash)."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main(
                [str(db_path), str(config_path), "9999", "--models", "stub-a,stub-b"]
            )

        assert exit_code == 1
        assert "not found" in stderr_buf.getvalue()


class TestBakeoffTwoModelEndToEnd:
    """End-to-end 2-model bakeoff with stubbed worktree/agent/diff helpers.

    This is the assertion set criterion 551 mandates: the report must contain
    one column per model plus the pairwise diff section.
    """

    def _install_stubs(self, monkeypatch, created_worktrees, spawned):
        """Replace git + agent shell-outs with in-memory stubs that record calls.

        Worktree creation and pairwise-diff collection both shell out; the test
        runs inside a tmp_path with no git history, so stubs let us assert the
        orchestration without needing a repo.
        """
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda repo_root: "main")

        def fake_create_worktree(repo_root, worktree_path, branch, base_branch):
            created_worktrees.append({
                "worktree": worktree_path,
                "branch": branch,
                "base": base_branch,
            })
            os.makedirs(worktree_path, exist_ok=True)
            return True, ""

        monkeypatch.setattr(tusk_bakeoff, "_create_worktree", fake_create_worktree)

        class _FakeProc:
            def __init__(self, model, shadow_id):
                self.pid = 1000 + shadow_id
                self.returncode = 0
                self._model = model

            def communicate(self):
                return (f"agent for {self._model} finished\n".encode(), b"")

        def fake_spawn(claude_bin, shadow_id, model, worktree_path, repo_root):
            spawned.append({
                "claude_bin": claude_bin,
                "shadow_id": shadow_id,
                "model": model,
                "worktree": worktree_path,
                "repo_root": repo_root,
            })
            return _FakeProc(model, shadow_id)

        monkeypatch.setattr(tusk_bakeoff, "_spawn_agent", fake_spawn)

        # Two-argument stub so the pairwise diff section has deterministic body.
        monkeypatch.setattr(
            tusk_bakeoff,
            "_pairwise_diff_stat",
            lambda repo_root, a, b: f"diff-stat({a}..{b})",
        )

    def test_2_model_bakeoff_emits_columns_and_pairwise_diff(
        self, db_path, config_path, monkeypatch
    ):
        task_id, src_crit_ids = _insert_source_task(db_path)
        created_worktrees: list[dict] = []
        spawned: list[dict] = []
        self._install_stubs(monkeypatch, created_worktrees, spawned)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path),
                str(config_path),
                str(task_id),
                "--models",
                "stub-a,stub-b",
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"
        stdout = stdout_buf.getvalue()

        # --- Shadow rows ----------------------------------------------------
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT id, summary, bakeoff_id, bakeoff_shadow "
                "FROM tasks WHERE bakeoff_shadow = 1 ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2, f"Expected 2 shadow rows, found {len(rows)}"
        bakeoff_ids = {r[2] for r in rows}
        assert len(bakeoff_ids) == 1, "Both shadows must share a single bakeoff_id"
        shadow_ids = [r[0] for r in rows]

        # --- Criteria cloned onto each shadow -------------------------------
        conn = sqlite3.connect(str(db_path))
        try:
            for sid in shadow_ids:
                crit = conn.execute(
                    "SELECT criterion FROM acceptance_criteria "
                    "WHERE task_id = ? ORDER BY id",
                    (sid,),
                ).fetchall()
                assert [c[0] for c in crit] == ["criterion A", "criterion B"], (
                    f"Shadow {sid} missing cloned criteria: {crit}"
                )
        finally:
            conn.close()

        # --- Worktree + spawn call accounting -------------------------------
        assert len(created_worktrees) == 2
        assert len(spawned) == 2
        spawned_models = {s["model"] for s in spawned}
        assert spawned_models == {"stub-a", "stub-b"}
        branch_names = {c["branch"] for c in created_worktrees}
        bakeoff_id = next(iter(bakeoff_ids))
        for sid in shadow_ids:
            assert any(
                f"feature/bakeoff-{bakeoff_id}-{sid}-" in b for b in branch_names
            ), f"No worktree branch encodes shadow {sid}: {branch_names}"

        # --- Report shape: one column per model + pairwise section ---------
        assert "# Bakeoff" in stdout
        header_line = next(
            (line for line in stdout.splitlines() if line.startswith("| Metric")),
            None,
        )
        assert header_line is not None, f"No metric header row in report:\n{stdout}"
        assert "stub-a" in header_line
        assert "stub-b" in header_line

        assert "## Pairwise diffs" in stdout
        assert "### stub-a vs stub-b" in stdout
        assert "diff-stat(" in stdout, "Pairwise diff stub output missing from report"
