"""Integration test for tusk bakeoff end-to-end (2-model attempt).

Covers:
- --models with fewer than 2 identifiers fails with a usage error.
- A 2-model bakeoff clones two shadow rows with a shared bakeoff_id and
  bakeoff_shadow=1, copying every acceptance criterion from the source task.
- The bakeoff command creates one git worktree per model on a deterministic
  branch name that encodes the bakeoff_id and shadow_id.
- The final markdown report contains one column per model plus a pairwise
  diff section — criterion 551's two required assertions.
- Worktree creation failure rolls back ALL previously-created worktrees AND
  the shadow rows, so a retry starts from a clean slate (review fix).
- A hung agent is killed after --timeout and its attempt is recorded as
  exit_code=-9 rather than blocking the bakeoff forever (review fix).
- tusk task-list hides shadows by default, --include-shadows re-includes
  them, and --bakeoff <id> filters to a single bakeoff (review fix).

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


class TestBakeoffWorktreeRollback:
    """Must-fix #1: if a worktree fails mid-setup, shadow rows + earlier worktrees roll back."""

    def test_worktree_failure_rolls_back_shadows_and_earlier_worktrees(
        self, db_path, config_path, monkeypatch
    ):
        task_id, _ = _insert_source_task(db_path)
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        created = []
        git_calls = []

        def _create(repo_root, worktree_path, branch, base_branch):
            # Succeed on the first attempt, fail on the second.
            if len(created) >= 1:
                return False, "simulated failure"
            created.append(worktree_path)
            os.makedirs(worktree_path, exist_ok=True)
            return True, ""

        def _fake_subprocess_run(args, **kwargs):
            git_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff, "_create_worktree", _create)
        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_subprocess_run)

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path), str(task_id),
                "--models", "stub-a,stub-b",
            ])

        assert exit_code == 2

        # Shadow rows must be rolled back by the transaction.
        conn = sqlite3.connect(str(db_path))
        try:
            shadow_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE bakeoff_shadow = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        assert shadow_count == 0, (
            f"Expected all shadow rows rolled back, found {shadow_count} remaining"
        )

        # Earlier-created worktrees must have been torn down.
        removes = [c for c in git_calls if c[:3] == ["git", "worktree", "remove"]]
        branch_dels = [c for c in git_calls if c[:2] == ["git", "branch"] and c[2] == "-D"]
        assert removes, f"Expected git worktree remove calls in {git_calls}"
        assert branch_dels, f"Expected git branch -D calls in {git_calls}"

        stderr = stderr_buf.getvalue()
        assert "Rolled back" in stderr


class TestBakeoffAgentTimeout:
    """Must-fix #3: a hung agent is killed after --timeout and reported as -9."""

    def test_timeout_kills_hung_agent_and_records_exit_minus_9(
        self, db_path, config_path, monkeypatch
    ):
        task_id, _ = _insert_source_task(db_path)
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")
        monkeypatch.setattr(
            tusk_bakeoff,
            "_create_worktree",
            lambda rr, wt, br, base: (os.makedirs(wt, exist_ok=True) or (True, "")),
        )
        monkeypatch.setattr(tusk_bakeoff, "_pairwise_diff_stat", lambda rr, a, b: "stub-diff")

        class _HangingProc:
            def __init__(self, shadow_id):
                self.pid = 5000 + shadow_id
                self.returncode = None
                self._killed = False

            def communicate(self, timeout=None):
                if not self._killed:
                    raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
                return (b"", b"killed")

            def kill(self):
                self._killed = True
                self.returncode = -9

        spawns = []

        def fake_spawn(claude_bin, shadow_id, model, worktree_path, repo_root):
            spawns.append(shadow_id)
            return _HangingProc(shadow_id)

        monkeypatch.setattr(tusk_bakeoff, "_spawn_agent", fake_spawn)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path), str(task_id),
                "--models", "stub-a,stub-b",
                "--timeout", "1",
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"
        assert len(spawns) == 2
        stderr_out = stderr_buf.getvalue()
        assert "killed on timeout" in stderr_out, stderr_out
        # Report row for agent exit should carry the -9 timeout marker for both.
        stdout = stdout_buf.getvalue()
        agent_exit_row = next(
            (line for line in stdout.splitlines() if line.startswith("| Agent exit")),
            None,
        )
        assert agent_exit_row is not None, f"No 'Agent exit' row in report:\n{stdout}"
        assert agent_exit_row.count("-9") == 2, (
            f"Expected both columns to show -9, got: {agent_exit_row}"
        )


class TestTaskListShadowFilters:
    """Suggest: task-list --include-shadows / --bakeoff <id> flag coverage."""

    def _insert_rows(self, db_path):
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "INSERT INTO tasks (summary, priority, complexity, priority_score) "
                "VALUES ('real task', 'Medium', 'S', 50)"
            )
            real_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO tasks (summary, priority, complexity, priority_score, "
                "bakeoff_id, bakeoff_shadow) "
                "VALUES ('shadow 1', 'Medium', 'S', 50, 1, 1)"
            )
            shadow1_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO tasks (summary, priority, complexity, priority_score, "
                "bakeoff_id, bakeoff_shadow) "
                "VALUES ('shadow 2', 'Medium', 'S', 50, 2, 1)"
            )
            shadow2_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return real_id, shadow1_id, shadow2_id

    def _list_ids(self, db_path, config_path, *extra_flags):
        import json as _json
        script = os.path.join(REPO_ROOT, "bin", "tusk-task-list.py")
        result = subprocess.run(
            ["python3", script, str(db_path), str(config_path), "--format", "json", *extra_flags],
            capture_output=True, text=True, check=True,
        )
        return {row["id"] for row in _json.loads(result.stdout)}

    def test_default_hides_shadows(self, db_path, config_path):
        real_id, s1, s2 = self._insert_rows(db_path)
        ids = self._list_ids(db_path, config_path)
        assert real_id in ids
        assert s1 not in ids
        assert s2 not in ids

    def test_include_shadows_shows_both(self, db_path, config_path):
        real_id, s1, s2 = self._insert_rows(db_path)
        ids = self._list_ids(db_path, config_path, "--include-shadows")
        assert {real_id, s1, s2} <= ids

    def test_bakeoff_id_filters_to_one_bakeoff(self, db_path, config_path):
        real_id, s1, s2 = self._insert_rows(db_path)
        ids = self._list_ids(db_path, config_path, "--bakeoff", "1")
        assert s1 in ids
        assert s2 not in ids
        assert real_id not in ids


class TestBakeoffIsolationClone:
    """TASK-125: --isolation=clone uses _create_clone (not _create_worktree),
    records isolation in the shadow description suffix, and fetches clone
    branches into repo_root post-agent so aggregation stays isolation-agnostic.
    """

    def test_clone_mode_wires_through_and_tags_shadows(
        self, db_path, config_path, monkeypatch, tmp_path
    ):
        task_id, _ = _insert_source_task(db_path)

        clone_calls: list[dict] = []
        worktree_calls: list[dict] = []
        fetch_calls: list[list] = []

        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        def fake_clone(repo_root, clone_path, branch, base_branch):
            clone_calls.append({
                "clone_path": clone_path, "branch": branch, "base": base_branch,
            })
            os.makedirs(clone_path, exist_ok=True)
            return True, ""

        def fake_worktree(repo_root, worktree_path, branch, base_branch):
            worktree_calls.append({
                "worktree": worktree_path, "branch": branch, "base": base_branch,
            })
            os.makedirs(worktree_path, exist_ok=True)
            return True, ""

        monkeypatch.setattr(tusk_bakeoff, "_create_clone", fake_clone)
        monkeypatch.setattr(tusk_bakeoff, "_create_worktree", fake_worktree)

        def fake_run(args, **kwargs):
            # Record `git fetch <clone_path> <branch>:<branch>` calls; stub
            # everything else (git pull/push etc.) as a no-op success.
            if isinstance(args, list) and len(args) >= 2 and args[:2] == ["git", "fetch"]:
                fetch_calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", fake_run)

        class _Proc:
            def __init__(self, shadow_id):
                self.pid = 2000 + shadow_id
                self.returncode = 0
            def communicate(self, timeout=None):
                return (b"", b"")

        monkeypatch.setattr(
            tusk_bakeoff, "_spawn_agent",
            lambda claude_bin, shadow_id, model, wt, rr: _Proc(shadow_id),
        )
        monkeypatch.setattr(tusk_bakeoff, "_pairwise_diff_stat", lambda rr, a, b: "stub")

        workspace_root = str(tmp_path / "bakeoffs")

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(stdout_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path), str(task_id),
                "--models", "stub-a,stub-b",
                "--isolation", "clone",
                "--workspace-root", workspace_root,
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"

        # Clone path taken, not worktree path.
        assert len(clone_calls) == 2
        assert worktree_calls == []

        # Shadow descriptions must record isolation=clone so pick/discard
        # diagnostics and post-mortem readers can tell which mode was used.
        conn = sqlite3.connect(str(db_path))
        try:
            descs = [
                row[0] for row in conn.execute(
                    "SELECT description FROM tasks WHERE bakeoff_shadow = 1 "
                    "ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert len(descs) == 2
        for d in descs:
            assert "isolation=clone" in d, f"isolation marker missing: {d!r}"

        # One fetch per clone attempt so repo_root sees the attempt branches.
        assert len(fetch_calls) == 2
        for call in fetch_calls:
            assert call[0] == "git" and call[1] == "fetch"
            assert call[3].count(":") == 1
            src, dst = call[3].split(":")
            assert src == dst
            assert src.startswith("feature/bakeoff-")


# ---------------------------------------------------------------------------
# pick / discard cleanup subcommands (TASK-124)
# ---------------------------------------------------------------------------


def _seed_bakeoff(
    db_path: str, models: tuple[str, ...] = ("stub-a", "stub-b")
) -> tuple[int, int, list[int]]:
    """Insert a source task + N shadow rows sharing a bakeoff_id.

    Returns (source_id, bakeoff_id, [shadow_ids]). Shadow descriptions include
    the `source=TASK-<src>` suffix that cmd_pick parses to locate the source.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO tasks (summary, description, status, task_type, priority, "
            "complexity, priority_score) "
            "VALUES ('source task', 'bakeoff source description', 'In Progress', "
            "'feature', 'Medium', 'S', 50)"
        )
        source_id = cur.lastrowid

        max_row = conn.execute(
            "SELECT COALESCE(MAX(bakeoff_id), 0) FROM tasks"
        ).fetchone()[0]
        bakeoff_id = int(max_row or 0) + 1

        shadow_ids = []
        for model in models:
            suffix = f"\n\n[bakeoff {bakeoff_id} attempt · model={model} · source=TASK-{source_id}]"
            cur = conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, "
                "domain, task_type, complexity, bakeoff_id, bakeoff_shadow, "
                "priority_score) "
                "VALUES (?, ?, 'To Do', 'Medium', NULL, 'feature', 'S', ?, 1, 50)",
                ("source task", "bakeoff source description" + suffix, bakeoff_id),
            )
            shadow_ids.append(cur.lastrowid)

        for sid in shadow_ids:
            conn.execute(
                "INSERT INTO acceptance_criteria (task_id, criterion, source) "
                "VALUES (?, 'criterion A', 'original')",
                (sid,),
            )
        conn.commit()
    finally:
        conn.close()
    return source_id, bakeoff_id, shadow_ids


def _open_session(db_path: str, task_id: int) -> int:
    """Open a task_sessions row (ended_at IS NULL) on the given task."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO task_sessions (task_id, started_at) "
            "VALUES (?, datetime('now'))",
            (task_id,),
        )
        sid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return sid


class TestBakeoffPickDiscardErrors:
    """Criteria 555 + 556: unknown bakeoff_id and open shadow sessions both refuse."""

    def test_pick_rejects_unknown_bakeoff_id(self, db_path, config_path):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main(
                [str(db_path), str(config_path), "pick", "9999", "1"]
            )
        assert exit_code == 1
        assert "bakeoff 9999 unknown" in stderr_buf.getvalue()

    def test_discard_rejects_unknown_bakeoff_id(self, db_path, config_path):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main(
                [str(db_path), str(config_path), "discard", "9999"]
            )
        assert exit_code == 1
        assert "bakeoff 9999 unknown" in stderr_buf.getvalue()

    def test_pick_rejects_shadow_not_in_bakeoff(self, db_path, config_path):
        _, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        foreign_shadow = max(shadow_ids) + 100
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "pick", str(bakeoff_id), str(foreign_shadow),
            ])
        assert exit_code == 1
        assert f"TASK-{foreign_shadow} is not a shadow" in stderr_buf.getvalue()

    def test_pick_refuses_when_shadow_session_open(self, db_path, config_path):
        _, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        _open_session(db_path, shadow_ids[0])

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "pick", str(bakeoff_id), str(shadow_ids[1]),
            ])
        assert exit_code == 1
        stderr = stderr_buf.getvalue()
        assert "open session" in stderr
        assert str(shadow_ids[0]) in stderr

    def test_discard_refuses_when_shadow_session_open(self, db_path, config_path):
        _, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        _open_session(db_path, shadow_ids[1])

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "discard", str(bakeoff_id),
            ])
        assert exit_code == 1
        stderr = stderr_buf.getvalue()
        assert "open session" in stderr
        assert str(shadow_ids[1]) in stderr


class TestBakeoffPick:
    """Criteria 552 + 553: pick merges chosen branch, closes source, prunes siblings."""

    def _install_stubs(self, monkeypatch, bakeoff_id: int, shadow_ids: list[int]):
        """Stub every git / tusk-bin subprocess call bakeoff pick/discard makes.

        Records subprocess.run invocations so the test can assert that
        session-close and task-done were issued against the right IDs and
        that each shadow branch got a worktree-remove + branch-D teardown.
        """
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        fake_branches = [
            f"feature/bakeoff-{bakeoff_id}-{sid}-stub" for sid in shadow_ids
        ]

        def _fake_find(repo_root, bid, sid=None):
            assert bid == bakeoff_id
            if sid is None:
                return list(fake_branches)
            return [b for b in fake_branches if f"-{sid}-" in b]

        monkeypatch.setattr(tusk_bakeoff, "_find_bakeoff_branches", _fake_find)
        monkeypatch.setattr(
            tusk_bakeoff, "_resolve_worktree_for_branch", lambda rr, br: None
        )
        monkeypatch.setattr(
            tusk_bakeoff,
            "_merge_shadow_branch",
            lambda rr, br, use_rebase=False: (True, ""),
        )

        calls: list[list] = []

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_run)
        return calls

    def test_pick_happy_path(self, db_path, config_path, monkeypatch):
        source_id, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        source_session_id = _open_session(db_path, source_id)
        chosen = shadow_ids[0]
        other = shadow_ids[1]

        calls = self._install_stubs(monkeypatch, bakeoff_id, shadow_ids)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "pick", str(bakeoff_id), str(chosen),
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"

        # Source session closed + source task-done issued via tusk-bin subprocess.
        flat = [" ".join(str(a) for a in c) for c in calls]
        assert any(
            f"session-close {source_session_id}" in c for c in flat
        ), f"Expected session-close call, got:\n{flat}"
        assert any(
            f"task-done {source_id} --reason completed" in c for c in flat
        ), f"Expected task-done call, got:\n{flat}"

        # All bakeoff branches torn down (-D issued once per shadow branch).
        branch_dels = [c for c in calls if c[:3] == ["git", "branch", "-D"]]
        assert len(branch_dels) == len(shadow_ids), (
            f"Expected {len(shadow_ids)} branch deletions, got {branch_dels}"
        )

        # Sibling shadow row deleted; chosen shadow row remains as audit trail.
        conn = sqlite3.connect(str(db_path))
        try:
            remaining = {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM tasks WHERE bakeoff_id = ?", (bakeoff_id,)
                ).fetchall()
            }
        finally:
            conn.close()
        assert chosen in remaining
        assert other not in remaining, "Sibling shadow should have been deleted"


class TestBakeoffPickRebase:
    """TASK-128: `tusk bakeoff pick --rebase` threads through and drives the rebase path."""

    def test_pick_threads_rebase_flag_through_to_merge_helper(
        self, db_path, config_path, monkeypatch
    ):
        """cmd_pick --rebase must call _merge_shadow_branch with use_rebase=True."""
        source_id, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        _open_session(db_path, source_id)
        chosen = shadow_ids[0]

        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")
        fake_branches = [
            f"feature/bakeoff-{bakeoff_id}-{sid}-stub" for sid in shadow_ids
        ]
        monkeypatch.setattr(
            tusk_bakeoff,
            "_find_bakeoff_branches",
            lambda rr, bid, sid=None: list(fake_branches) if sid is None
            else [b for b in fake_branches if f"-{sid}-" in b],
        )
        monkeypatch.setattr(
            tusk_bakeoff, "_resolve_worktree_for_branch", lambda rr, br: None
        )

        captured: dict = {}

        def _fake_merge(repo_root, branch, use_rebase=False):
            captured["use_rebase"] = use_rebase
            captured["branch"] = branch
            return True, ""

        monkeypatch.setattr(tusk_bakeoff, "_merge_shadow_branch", _fake_merge)
        monkeypatch.setattr(
            tusk_bakeoff.subprocess, "run",
            lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "pick", str(bakeoff_id), str(chosen), "--rebase",
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"
        assert captured.get("use_rebase") is True, (
            f"Expected use_rebase=True to reach _merge_shadow_branch, "
            f"got {captured!r}"
        )
        assert f"-{chosen}-" in captured["branch"]

    def test_pick_defaults_rebase_flag_to_false(
        self, db_path, config_path, monkeypatch
    ):
        """Without --rebase, _merge_shadow_branch must be called with use_rebase=False."""
        source_id, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        _open_session(db_path, source_id)
        chosen = shadow_ids[0]

        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")
        fake_branches = [
            f"feature/bakeoff-{bakeoff_id}-{sid}-stub" for sid in shadow_ids
        ]
        monkeypatch.setattr(
            tusk_bakeoff,
            "_find_bakeoff_branches",
            lambda rr, bid, sid=None: list(fake_branches) if sid is None
            else [b for b in fake_branches if f"-{sid}-" in b],
        )
        monkeypatch.setattr(
            tusk_bakeoff, "_resolve_worktree_for_branch", lambda rr, br: None
        )

        captured: dict = {}

        def _fake_merge(repo_root, branch, use_rebase=False):
            captured["use_rebase"] = use_rebase
            return True, ""

        monkeypatch.setattr(tusk_bakeoff, "_merge_shadow_branch", _fake_merge)
        monkeypatch.setattr(
            tusk_bakeoff.subprocess, "run",
            lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "pick", str(bakeoff_id), str(chosen),
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"
        assert captured.get("use_rebase") is False

    def test_merge_shadow_branch_rebase_runs_rebase_before_ff_merge(
        self, monkeypatch, tmp_path
    ):
        """With use_rebase=True, the helper checks out the branch, rebases onto
        the default, switches back, then ff-merges — the sequence tusk-merge
        executes under the same flag."""
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        calls: list[list] = []

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_run)

        ok, err = tusk_bakeoff._merge_shadow_branch(
            str(tmp_path), "feature/bakeoff-1-2-stub", use_rebase=True
        )
        assert ok, f"rebase path should succeed when all git calls succeed: {err}"

        seq = [c[:3] for c in calls if c[:1] == ["git"]]
        # Sequence must include: checkout main -> checkout branch -> rebase main ->
        # checkout main -> merge --ff-only branch (the rebase bracket lives
        # between the first default-branch checkout and the ff-only merge).
        assert ["git", "checkout", "main"] in seq
        assert ["git", "checkout", "feature/bakeoff-1-2-stub"] in seq
        assert ["git", "rebase", "main"] in seq
        assert ["git", "merge", "--ff-only"] in seq, (
            f"ff-only merge must still run after a successful rebase: seq={seq}"
        )
        # Order check: rebase must happen before the ff-only merge.
        rebase_idx = next(
            i for i, c in enumerate(calls) if c[:3] == ["git", "rebase", "main"]
        )
        merge_idx = next(
            i for i, c in enumerate(calls) if c[:3] == ["git", "merge", "--ff-only"]
        )
        assert rebase_idx < merge_idx, (
            f"rebase must precede ff-only merge; rebase@{rebase_idx} merge@{merge_idx}"
        )

    def test_merge_shadow_branch_rebase_conflict_aborts_and_surfaces_error(
        self, monkeypatch, tmp_path
    ):
        """A failing `git rebase` must trigger `git rebase --abort` and return False.

        Without the abort, the repo would be left in the middle of a rebase
        and the caller would have no idea the state needed cleanup.
        """
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        calls: list[list] = []

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:3] == ["git", "rebase", "main"]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="CONFLICT (content): Merge conflict"
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_run)

        ok, err = tusk_bakeoff._merge_shadow_branch(
            str(tmp_path), "feature/bakeoff-1-2-stub", use_rebase=True
        )
        assert not ok
        assert "rebase" in err.lower()
        assert ["git", "rebase", "--abort"] in [c[:3] for c in calls], (
            f"rebase --abort must run after a failed rebase; calls={calls}"
        )
        # ff-only merge must NOT have been attempted once rebase failed.
        assert ["git", "merge", "--ff-only"] not in [c[:3] for c in calls]

    def test_merge_shadow_branch_ff_failure_without_rebase_hints_flag(
        self, monkeypatch, tmp_path
    ):
        """When ff-only fails and --rebase wasn't used, the error must suggest it."""
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        def _fake_run(args, **kwargs):
            if args[:3] == ["git", "merge", "--ff-only"]:
                return subprocess.CompletedProcess(
                    args, 128, stdout="",
                    stderr="fatal: Not possible to fast-forward, aborting.",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_run)

        ok, err = tusk_bakeoff._merge_shadow_branch(
            str(tmp_path), "feature/bakeoff-1-2-stub", use_rebase=False
        )
        assert not ok
        assert "--rebase" in err, (
            f"Error must point users at the --rebase escape hatch; got:\n{err}"
        )

    def test_merge_shadow_branch_ff_failure_with_rebase_does_not_rehint(
        self, monkeypatch, tmp_path
    ):
        """After --rebase was already used, a subsequent ff failure must NOT re-suggest --rebase."""
        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")

        def _fake_run(args, **kwargs):
            if args[:3] == ["git", "merge", "--ff-only"]:
                return subprocess.CompletedProcess(
                    args, 128, stdout="", stderr="fatal: Not possible to fast-forward",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_run)

        ok, err = tusk_bakeoff._merge_shadow_branch(
            str(tmp_path), "feature/bakeoff-1-2-stub", use_rebase=True
        )
        assert not ok
        assert "--rebase" not in err, (
            f"Should not re-suggest --rebase when it was already used; got:\n{err}"
        )


class TestBakeoffDiscard:
    """Criterion 554: discard deletes all shadow rows + worktrees, source untouched."""

    def test_discard_happy_path(self, db_path, config_path, monkeypatch):
        source_id, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)

        monkeypatch.setattr(tusk_bakeoff, "_detect_default_branch", lambda rr: "main")
        fake_branches = [
            f"feature/bakeoff-{bakeoff_id}-{sid}-stub" for sid in shadow_ids
        ]
        monkeypatch.setattr(
            tusk_bakeoff, "_find_bakeoff_branches",
            lambda rr, bid, sid=None: list(fake_branches) if sid is None
            else [b for b in fake_branches if f"-{sid}-" in b],
        )
        monkeypatch.setattr(
            tusk_bakeoff, "_resolve_worktree_for_branch", lambda rr, br: None
        )

        calls: list[list] = []

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(tusk_bakeoff.subprocess, "run", _fake_run)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = tusk_bakeoff.main([
                str(db_path), str(config_path),
                "discard", str(bakeoff_id),
            ])

        assert exit_code == 0, f"stderr:\n{stderr_buf.getvalue()}"

        # Source task untouched; every shadow row gone.
        conn = sqlite3.connect(str(db_path))
        try:
            src_rows = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (source_id,)
            ).fetchall()
            shadow_rows = conn.execute(
                "SELECT id FROM tasks WHERE bakeoff_id = ? AND bakeoff_shadow = 1",
                (bakeoff_id,),
            ).fetchall()
        finally:
            conn.close()
        assert len(src_rows) == 1, "Source task row must remain untouched"
        assert shadow_rows == [], f"Every shadow row must be deleted, found {shadow_rows}"

        # Every bakeoff branch got a -D; no session-close or task-done subprocess call
        # was issued (discard never touches the source task).
        branch_dels = [c for c in calls if c[:3] == ["git", "branch", "-D"]]
        assert len(branch_dels) == len(shadow_ids)
        flat = [" ".join(str(a) for a in c) for c in calls]
        assert not any("session-close" in c for c in flat), (
            "discard must not close any source session"
        )
        assert not any("task-done" in c for c in flat), (
            "discard must leave the source task untouched"
        )


class TestDeleteShadowRowsChildCleanup:
    """TASK-127: _delete_shadow_rows must sweep the full child set.

    Seeds every table listed in the deferred finding — task_sessions,
    task_progress, skill_runs, code_reviews, review_comments (both review_id
    and deferred_task_id paths), tool_call_stats, tool_call_events — against
    a shadow task, calls _delete_shadow_rows directly, and asserts each child
    row is gone. Sidesteps open-session refusals and git/worktree plumbing by
    exercising the helper in isolation.
    """

    def test_deletes_every_child_row_for_shadow(self, db_path, config_path):
        source_id, bakeoff_id, shadow_ids = _seed_bakeoff(db_path)
        target = shadow_ids[0]
        sibling = shadow_ids[1]

        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "INSERT INTO task_sessions (task_id, started_at, ended_at) "
                "VALUES (?, datetime('now', '-1 hour'), datetime('now'))",
                (target,),
            )
            session_id = cur.lastrowid

            conn.execute(
                "INSERT INTO task_progress (task_id, commit_hash, next_steps) "
                "VALUES (?, 'deadbeef', 'resume here')",
                (target,),
            )

            cur = conn.execute(
                "INSERT INTO skill_runs (skill_name, started_at, task_id) "
                "VALUES ('tusk', datetime('now'), ?)",
                (target,),
            )
            skill_run_id = cur.lastrowid

            cur = conn.execute(
                "INSERT INTO code_reviews (task_id, reviewer, status) "
                "VALUES (?, 'stub-reviewer', 'approved')",
                (target,),
            )
            review_id = cur.lastrowid

            conn.execute(
                "INSERT INTO review_comments (review_id, comment) "
                "VALUES (?, 'direct child via review_id')",
                (review_id,),
            )

            # deferred_task_id back-reference: a review on the SOURCE task
            # whose comment points at the shadow as the deferred follow-up.
            cur = conn.execute(
                "INSERT INTO code_reviews (task_id, reviewer, status) "
                "VALUES (?, 'stub-reviewer', 'approved')",
                (source_id,),
            )
            foreign_review_id = cur.lastrowid
            conn.execute(
                "INSERT INTO review_comments (review_id, comment, "
                "resolution, deferred_task_id) "
                "VALUES (?, 'back-ref via deferred_task_id', 'deferred', ?)",
                (foreign_review_id, target),
            )

            conn.execute(
                "INSERT INTO tool_call_stats "
                "(session_id, task_id, tool_name, call_count) "
                "VALUES (?, ?, 'Read', 3)",
                (session_id, target),
            )
            conn.execute(
                "INSERT INTO tool_call_stats "
                "(skill_run_id, tool_name, call_count) "
                "VALUES (?, 'Edit', 2)",
                (skill_run_id,),
            )
            conn.execute(
                "INSERT INTO tool_call_events "
                "(session_id, task_id, tool_name, called_at) "
                "VALUES (?, ?, 'Read', datetime('now'))",
                (session_id, target),
            )
            conn.execute(
                "INSERT INTO tool_call_events "
                "(skill_run_id, tool_name, called_at) "
                "VALUES (?, 'Edit', datetime('now'))",
                (skill_run_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # Invoke the helper directly against the target shadow.
        conn = tusk_bakeoff.get_connection(str(db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            tusk_bakeoff._delete_shadow_rows(conn, [target])
            conn.commit()
        finally:
            conn.close()

        conn = sqlite3.connect(str(db_path))
        try:
            def scalar(sql, *params):
                return conn.execute(sql, params).fetchone()[0]

            # Target shadow and all of its direct children: gone.
            assert scalar(
                "SELECT COUNT(*) FROM tasks WHERE id = ?", target
            ) == 0, "shadow task row must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM acceptance_criteria WHERE task_id = ?",
                target,
            ) == 0
            assert scalar(
                "SELECT COUNT(*) FROM task_sessions WHERE task_id = ?", target
            ) == 0, "task_sessions row must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM task_progress WHERE task_id = ?", target
            ) == 0, "task_progress row must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM skill_runs WHERE task_id = ? OR id = ?",
                target, skill_run_id,
            ) == 0, "skill_runs row must be hard-deleted (not just SET NULL'd)"
            assert scalar(
                "SELECT COUNT(*) FROM code_reviews WHERE task_id = ?", target
            ) == 0, "code_reviews row must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM review_comments WHERE review_id = ?",
                review_id,
            ) == 0, "review_comments via review_id must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM review_comments WHERE deferred_task_id = ?",
                target,
            ) == 0, "review_comments back-ref via deferred_task_id must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM tool_call_stats "
                "WHERE task_id = ? OR session_id = ? OR skill_run_id = ?",
                target, session_id, skill_run_id,
            ) == 0, "tool_call_stats rows must be deleted"
            assert scalar(
                "SELECT COUNT(*) FROM tool_call_events "
                "WHERE task_id = ? OR session_id = ? OR skill_run_id = ?",
                target, session_id, skill_run_id,
            ) == 0, "tool_call_events rows must be deleted"

            # Sibling shadow (not in the delete list) and source task both
            # survive — the helper must not touch unrelated rows.
            assert scalar(
                "SELECT COUNT(*) FROM tasks WHERE id = ?", sibling
            ) == 1, "sibling shadow must remain"
            assert scalar(
                "SELECT COUNT(*) FROM tasks WHERE id = ?", source_id
            ) == 1, "source task must remain"
            assert scalar(
                "SELECT COUNT(*) FROM code_reviews WHERE task_id = ?",
                source_id,
            ) == 1, (
                "source-task code_review must remain — only the "
                "review_comments row back-referencing the shadow is deleted, "
                "the unrelated parent review stays put"
            )
        finally:
            conn.close()
