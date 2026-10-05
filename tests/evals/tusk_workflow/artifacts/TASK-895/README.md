# TASK-895 retained evidence

`final-matrix.tar.gz` is the primary comparison: six fixtures, two prompt variants,
two repetitions, fixed gpt-6-astra/medium settings. `final-manifest.json`,
`final-summary.json` and `final-results.json` provide readable entry points.
`index.json` records archive checksums and file sizes.

`development-matrix.tar.gz` preserves the preceding 24 attempts, their original
scores, the frozen runner, and a separate correction of all scores. It is excluded
from primary comparison totals. The three pilot archives retain the sandbox
startup failure and the subsequent successful tiny-fix runs.

Each matrix archive contains complete requests and raw JSONL, per-call telemetry,
action/observation traces, initial and final task state, the disposable repositories
and databases, pinned prompt sources, and fixture/runner snapshots. Credentials
are excluded. Git remotes inside these archives point only to their original
disposable local paths; no publishing credentials or external remote is included.

To inspect without model calls, extract the final archive to a new directory
outside every Git checkout, then invoke the checked-in runner from this source
revision:

```bash
mkdir /private/tmp/tusk-895-final-audit
tar -xzf tests/evals/tusk_workflow/artifacts/TASK-895/final-matrix.tar.gz \
  -C /private/tmp/tusk-895-final-audit
python3 tests/evals/tusk_workflow/runner.py --rescore /private/tmp/tusk-895-final-audit
```

Rescoring uses sandboxed local
tests, validates input hashes, grades every planned cell, and writes new files
without altering original results. It needs macOS and Python 3.11 or later but
makes no model calls and needs no authentication. The runner snapshots inside the
bundles are audit copies; use the repository layout to execute the runner.

See [the report](../../../../../docs/evaluations/tusk-workflow-prompts.md) for
recommendations, calibration history and limits.
