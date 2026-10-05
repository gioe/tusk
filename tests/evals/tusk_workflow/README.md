# Tusk workflow behavior evaluations

This suite compares versioned workflow instructions on six small, executable
scenarios. It is a controlled evaluation of workflow decisions, not an
end-to-end benchmark of the Tusk CLI or a substitute for its integration tests.

The model chooses actions through a constrained JSON protocol. The harness
executes allowed file edits, tests, and task-state transitions in a fresh Git
repository and task database, then returns observations. Delegation makes a real
second model call with the same model and reasoning settings. Grades use the
executed action trace, test results, and final repository/database state; a
model's claim that it finished is insufficient.

## What changes between variants

The baseline is the Codex Tusk workflow at distribution version 1280.
The candidate changes only exploration routing: precisely scoped XS/S work can
be explored locally, while larger or ambiguous work still requires delegation.
Dependent references to mandatory exploration are changed together. Implementation
routing, regression-test requirements, verification, and cleanup contracts stay
the same. Neither variant changes the production skill files.

Each attempt records the prompt and recovery-companion hashes, fixture hash,
model and settings, repetition, raw responses, executed actions and observations,
completion grade, false-completion grade, interventions, elapsed time, and token
usage. Unavailable pricing is reported as unavailable. Failed or missing attempts
remain failures; they are never silently removed from the denominator.

## Isolation

Runs use disposable repositories without an external publishing remote. Task databases,
state directories, worktree roots, and the agent's configuration home are isolated
from the live project. The model process uses Codex's read-only sandbox with
execution and external application tools disabled. The harness is the only
component allowed to apply actions. Model-authored tests run under an OS sandbox
that denies network access and all writes, including writes to the fixture database.
The partial-cleanup fixture has only a local, disposable bare Git remote.

The initial live runner supports macOS with `sandbox-exec` and fails closed when
that sandbox is unavailable. It never falls back to unrestricted execution.
Credentials needed for model inference are not part of saved run artifacts.

## Run and reproduce

Requirements: Python 3.11 or later, Git, an authenticated Codex CLI supporting the recorded
flags, and macOS `sandbox-exec`. The runner makes model calls using the selected
account; it does not estimate an unverified dollar price. Unit tests never call
models.

```bash
python3 -m pytest tests/unit/test_tusk_workflow_prompt_eval.py -q
python3 tests/evals/tusk_workflow/runner.py \
  --output /private/tmp/tusk-workflow-comparison-NEW \
  --model gpt-6-astra --reasoning medium \
  --repetitions 2 --max-turns 6 --timeout 120 --workers 2
```

Choose a new output directory outside every source checkout. Existing output
is refused rather than overwritten. `--case tiny-fix --variant
conditional-exploration --repetitions 1` runs a pilot. A complete comparison
omits these filters. Repetition 2 reverses the variant order; all requested
cells are written to the manifest before execution.

The baseline source commit is pinned in `runner.py`; source files are loaded
with `git show`, so that commit must be available locally. The candidate is a
fail-closed exact replacement of three passages in one instruction group. A
changed source that no longer matches the patch is an error. The manifest
records the CLI version, runner hash, source reference, model settings, prompt
hashes, fixture hash and complete run plan. Each attempt retains raw requests,
JSONL responses, observations, initial/final state, grades and usage. Delegated
calls are included in its duration and token totals. Missing usage remains null.

To audit saved behavior without another model call, unpack a retained bundle
outside every checkout, then run:

```bash
python3 tests/evals/tusk_workflow/runner.py --rescore /private/tmp/unpacked-run
```

Rescoring checks prompt/source/fixture identities, grades every planned attempt,
and writes separate `results-rescored.json` and `summary-rescored.json` files.
It records the grader hash/version and preserves the original grades and runtime
failures. It refuses to overwrite an existing rescore. Do not interpret rescoring
as rerunning the model or completing an interrupted attempt.

Recognized reconnect warnings are retained in call telemetry and accepted only
when the process actually completes a successful turn. Failed turns, unknown
errors, malformed responses and missing completion remain failures.

Read `summary.json` and `results.json`; process completion alone does not mean
the model passed every fixture. Setup failures, timeouts, invalid actions and
missing results cannot increase the success count. Keep failed attempts in the
report, and distinguish harness-development pilots from a frozen comparison.

## Interpreting results

Two repetitions per fixture and variant are a smoke comparison, not a statistically
powered benchmark. Report individual failures and paired outcomes before averages.
Clarification on an ambiguous fixture is a correct outcome even though it does
not finish implementation. Successful shipment with incomplete cleanup is distinct
from an unsuccessful merge. A timeout or malformed response is an execution
failure, not evidence that the prompt safely declined the task.

Duration includes model and harness overhead; token totals include delegated calls
when available. The constrained protocol removes shell navigation and much CLI
overhead, so these measurements cannot establish production latency or cost
savings. Fixed model names are recorded request targets, not a guarantee that a
provider never updates the underlying model. Repetitions can still vary.

Keep a production instruction unless the measured evidence supports changing it.
A candidate winning this suite is a reason to run a broader end-to-end comparison,
not authorization to rewrite production policy.

## Methodology references

- [OpenAI: Testing Agent Skills Systematically with Evals](https://developers.openai.com/blog/eval-skills)
  motivates capturing execution traces and checking observable outcomes.
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
  documents JSONL events, structured responses, and usage reporting.

The checked-in report is [tusk-workflow-prompts.md](../../../docs/evaluations/tusk-workflow-prompts.md).
