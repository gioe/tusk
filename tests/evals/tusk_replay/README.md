# Historical Tusk prompt replay pilot

This harness compares the full Codex workflow pinned at release 1280 with
`compact.md`, using two real historical bug snapshots and actual Codex shell
tools. Both variants retain delegated exploration. It tests the implementation,
verification, task-aware commit, and progress phase. The coordinator owns setup,
task start, workspace creation, and later release/merge/retro. Tasks must remain
In Progress. This is not a full-lifecycle or production-savings benchmark.

## Run

Requires macOS, the Codex CLI with named filesystem permission profiles,
Python 3.11+, pytest, Git, and Codex authentication. No dependencies are installed
by the runner. Output must be a **new directory outside every Git checkout**.

```bash
python3 tests/evals/tusk_replay/runner.py \
  --output /private/tmp/tusk-replay-calibration-NEW --calibrate-only

python3 tests/evals/tusk_replay/runner.py \
  --output /private/tmp/tusk-replay-pilot-NEW \
  --model gpt-6-astra --reasoning medium --repetitions 1 --timeout 1200
```

The second command consumes model usage. Optional repeatable `--case` filters are
`inline-python` and `nested-cache`; `--variant` accepts `full` and `compact`.
Each live invocation recalibrates its graders before any model calls. All
planned cells, including execution failures and timeouts, remain in results.
There is no unsandboxed fallback.
Calibration also creates each task/worktree and executes `tusk task-get` through
the restricted shell, catching CLI startup failures before consuming model usage.

## Isolation and evidence

- Historical trees come from `git archive`, followed by fresh Git initialization.
  Future commits, completed task records, and known solutions are absent.
- Automatic historical agent configuration is removed. Ordinary source skills
  and documentation remain so the source tree is realistic; trace review should
  note any consultation of competing workflow instructions.
- Each attempt has its own real Tusk database, task session, recorded task
  worktree, and local bare Git remote. Environment variables do not inherit live
  project pins, Git configuration, credentials, or Python import paths.
- The configured commit gate runs the case's existing focused suite. Removing
  agent configuration invalidates unrelated historical prompt-presence tests,
  so a full-suite comparison would introduce artificial failures. Agents must
  additionally execute any regression tests they add.
- The historical CLI has two BSD `mktemp -t` calls that ignore `TMPDIR` on macOS.
  Fixture setup adds `-p "${TMPDIR:?}"` to those calls and commits the adaptation
  before the agent starts. Both variants receive the same change. It avoids
  granting access to host temporary directories and does not alter this repo's
  production CLI. The manifest records all common fixture adaptations.
- Tools use a deny-by-default filesystem profile, with writes only inside the
  disposable execution tree, explicit Git metadata writes, minimal system reads,
  and Python runtime reads. The network is disabled. A real sandbox probe must
  prove source, user auth, isolated auth, evaluator, and sibling-artifact reads
  are denied, plus outside writes and network connections, before each call.
- The authenticated CLI supervisor uses a temporary credential directory outside
  the tool-readable tree. Credentials are never stored in run artifacts.
- Hidden behavioral graders run only after agents stop, under their own sandbox
  with the trusted grader readable. Calibration proves failures on pre-fix trees
  and passes on known fixes. Candidate-authored tests do not define these grades.

`manifest.json` records revisions, prompt/grader/runner hashes, model settings,
and the complete plan. `sources/` preserves the exact runner, grader, fixtures,
and compact prompt used in that invocation. Each attempt saves its request, CLI argv, raw event trace,
stderr, sandbox probe, hidden grader output, final diff, Git status, task state,
and result. Timeouts kill the complete process group.

`grade.correct` describes independent behavioral checks. Overall `correct` also
requires a successful completed model run, an implementation commit, progress,
and the open-task phase boundary. Manually audit event traces to verify a
meaningful agent-authored regression actually failed before the fix and passed
afterward, delegation occurred, and no claim exceeds observed evidence. The
runner does **not** automatically prove those workflow behaviors.

Parent CLI usage is recorded as `parent_reported_usage`. Combined parent/child
usage and cost remain null because the CLI stream does not establish inclusion
of child consumption. Do not compare token efficiency using incomplete totals.
Two known small bugs with one repetition each validate the harness; they do not
establish a general prompt-quality ranking or replace held-out real tasks.
