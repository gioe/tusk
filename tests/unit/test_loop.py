"""Unit tests for tusk-loop.py — drain-then-propose dispatch decision.

Covers the dispatch-decision node IDs surfaced by `decide_dispatch`:
- propose_on_empty — no ready task → surface `tusk propose-work` candidates,
                     do NOT auto-create (TASK-714 criterion #3338 / #3336)
- chain            — a ready chain head → /chain dispatch
- tusk             — a ready standalone task → /tusk dispatch

`decide_dispatch` is a pure function (no DB, no subprocess), so these tests
exercise the branch logic directly. `propose_work` is exercised with a stubbed
subprocess to confirm proposals are *surfaced* and never inserted.
"""

import importlib.util
import os
import sys
from unittest import mock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")

# tusk-loop.py does `sys.path.insert(0, dirname(__file__))` then `import tusk_loader`,
# so the bin dir must be importable for the module load to succeed.
if BIN not in sys.path:
    sys.path.insert(0, BIN)

_spec = importlib.util.spec_from_file_location(
    "tusk_loop",
    os.path.join(BIN, "tusk-loop.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# --- decide_dispatch: the dispatch-on-empty decision -------------------------

def test_propose_on_empty_when_no_ready_task():
    """No ready task → node is propose_on_empty (not a stop, not a dispatch)."""
    decision = mod.decide_dispatch(None, chain_head=False)
    assert decision["node"] == "propose_on_empty"
    # No skill is dispatched — the operator reviews proposals, nothing auto-runs.
    assert decision["skill"] is None
    assert decision["task_id"] is None


def test_propose_on_empty_ignores_chain_head_flag():
    """With no task, the chain_head flag is irrelevant — still propose_on_empty."""
    decision = mod.decide_dispatch(None, chain_head=True)
    assert decision["node"] == "propose_on_empty"


def test_chain_head_dispatches_chain():
    decision = mod.decide_dispatch({"id": 5}, chain_head=True)
    assert decision["node"] == "chain"
    assert decision["skill"] == "chain"
    assert decision["task_id"] == 5


def test_standalone_dispatches_tusk():
    decision = mod.decide_dispatch({"id": 9}, chain_head=False)
    assert decision["node"] == "tusk"
    assert decision["skill"] == "tusk"
    assert decision["task_id"] == 9


# --- propose_work: surfaces, never auto-creates ------------------------------

def test_propose_on_empty_surfaces_candidates_without_creating(capsys):
    """propose_work runs `tusk propose-work` (read-only) and prints the ranked
    candidates. It must never invoke a task-inserting command — the human gate
    on task origination is preserved (criterion #3336)."""
    fake = mock.Mock(returncode=0, stdout='[{"source":"todo_scan","score":45.0}]', stderr="")
    with mock.patch.object(mod.subprocess, "run", return_value=fake) as run:
        rc = mod.propose_work()

    assert rc == 0
    # Exactly one subprocess call, and it is the read-only propose-work command.
    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv[:2] == ["tusk", "propose-work"]
    # No task-creating verbs are ever issued.
    assert not any(verb in argv for verb in ("task-insert", "task-update", "create-task"))

    out = capsys.readouterr().out
    assert "todo_scan" in out
    # Wording makes the human gate explicit.
    assert "/create-task" in out


def test_propose_on_empty_handles_empty_proposals(capsys):
    """An empty proposal array is surfaced as a clean message, not an error."""
    fake = mock.Mock(returncode=0, stdout="[]", stderr="")
    with mock.patch.object(mod.subprocess, "run", return_value=fake):
        rc = mod.propose_work()
    assert rc == 0
    assert "no work proposals" in capsys.readouterr().out.lower()
