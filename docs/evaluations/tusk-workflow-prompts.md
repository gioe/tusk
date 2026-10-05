# Tusk workflow prompt evaluation — TASK-895

## Question and comparison

Does permitting local exploration for precisely scoped XS/S tasks preserve the
workflow decisions we care about, while reducing overhead in a controlled
behavioral evaluation?

The baseline is the Codex workflow at release **1280**, source commit
`060b7b3e0ac6cc004f957252a3b765e4ab29bfe1`. The candidate changes one instruction
group: exploration routing, including references that depend on that routing.
Implementation delegation policy and completion safeguards are unchanged. The
production prompts are not modified by this evaluation.

The fixture set covers missing regression tests, tiny fixes, ambiguous changes,
pre-existing failures, interrupted sessions, and partial merge cleanup. See the
[runner documentation](../../tests/evals/tusk_workflow/README.md) for execution,
isolation, and scoring details.

## Primary results — 2026-10-05

**Both variants produced 12/12 correct outcomes, with zero false completions.**
Conditional exploration used **26.7% fewer input tokens** in this harness.
Retain production routing for now; the candidate merits a larger, end-to-end
comparison before adoption. This is evidence of usefulness on these six scenarios,
not proof of general effectiveness or equivalence to past model generations.

Settings: requested model **gpt-6-astra**, reasoning **medium**, Codex CLI
**0.159.2**, two repetitions, two workers, six coordinator turns maximum and a
120-second timeout per model call. Pair order reverses for the second repetition.
The full matrix uses the same fixtures, prompt sources, model settings and runner.

| Measure | Baseline | Conditional exploration |
|---|---:|---:|
| Correct outcomes | 12/12 | 12/12 |
| False completions | 0 | 0 |
| Clarification interventions | 2 | 2 |
| Actual delegated consultations | 6 | 0 |
| Model calls, including consultations | 41 | 30 |
| Input tokens | 1,009,021 | 739,802 |
| Cached input tokens (included above) | 279,936 | 215,680 |
| Output tokens | 4,553 | 3,129 |
| Mean elapsed seconds per attempt | 30.1 | 22.4 |
| Calls with recovered transport warnings | 2 | 0 |
| Dollar cost | unavailable | unavailable |

The two interventions per variant were the expected requests for missing cache
policy. They are correct clarification outcomes, not unnecessary approval pauses.
Each variant completed eight implementation/verification phases, clarified two
ambiguous cases, and acknowledged two already-shipped cases with cleanup pending.

| Fixture | Baseline correct | Candidate correct | Mean seconds: baseline / candidate |
|---|---:|---:|---:|
| missing-regression | 2/2 | 2/2 | 50.9 / 36.1 |
| tiny-fix | 2/2 | 2/2 | 42.0 / 26.4 |
| ambiguous-change | 2/2 | 2/2 | 8.9 / 8.7 |
| pre-existing-failure | 2/2 | 2/2 | 58.9 / 39.6 |
| interrupted-session | 2/2 | 2/2 | 12.7 / 14.4 |
| partial-merge-cleanup | 2/2 | 2/2 | 7.1 / 8.9 |

The candidate reduced overhead where exploration could be local. Resume and
cleanup phases did not need exploration in either variant. Two baseline calls
had recovered network warnings, so elapsed-time differences are confounded by
transport behavior. Token totals come from reported CLI usage, include child
calls, and do not invent usage for unreported retries. The protocol replays the
full prompt for each coordinator call; avoided calls therefore have a larger
context effect than they may in a persistent production session. No pricing was
verified, and no dollar saving is claimed.

## Recommendation by instruction group

- **Exploration routing (measured): retain the production policy for now.** The
  conditional candidate achieved the same observed quality and intervention count
  with fewer calls and tokens. Six tiny fixtures and two repetitions are too weak
  to justify a general policy change; the protocol also supplies file contents,
  reducing the potential benefit of independent repository exploration. Expand
  to realistic repositories and ambiguous but actionable work before adoption.
- **Implementation routing, regression proof and recovery rules (not varied):
  retain them.** This comparison checks associated outcomes but does not isolate
  the value of each rule. It provides no causal basis to remove or rewrite them.

## Evidence and verification

[Retained evidence](../../tests/evals/tusk_workflow/artifacts/TASK-895/README.md)
includes readable manifests/results, complete compressed traces, prompt and fixture
hashes, disposable repositories/databases, and both runner versions. All primary
cells are present; no failed attempt was dropped from a reported denominator.
The development matrix's separate rescore yields 18 correct outcomes, no false
completions, and six incomplete attempts; those are not pooled into primary totals.

The harness has 27 focused tests covering grading validity, recovered versus fatal
transport errors, missing and duplicate results, additive regression coverage,
zero/all-skipped execution, unsafe paths, actual OS isolation, and rescore integrity.
The retained final archive was extracted and rescored without model calls; all
24 outcomes were reproduced. The repository unit gate is also required before merge. Production prompt files
are unchanged. Artifacts and documented limits are part of this task's deliverable.

## Limits of the experiment

This is a phase-scoped action-protocol smoke evaluation. It uses real model
responses, executable Python tests, Git repositories and disposable task state,
but it does not exercise the full Tusk CLI, review orchestration or publishing.
The protocol removes much of the shell navigation and integration overhead.
Fixtures are deliberately small and their task descriptions identify the relevant
contract; they are easier and less ambiguous than many production tasks.

Two repetitions per fixture/variant can surface failures but cannot establish
statistical equivalence or a general productivity improvement. Fixed model and
reasoning settings control the comparison at the request level; backend model
updates and scheduling/caching still introduce variation. Measured latency and
tokens are properties of this harness. No dollar savings can be inferred from
unavailable pricing.

This evaluation cannot answer whether the prompt works as well on earlier model
generations, and it does not measure the quality effect of TASK-894's conditional
recovery extraction. Both variants receive the same recovery rules. Those claims
require separate comparisons that change only the instruction group in question.

## Calibration and retained failures

Before the primary comparison, three tiny-fix pilots tested isolation and real
delegation. The first encountered a Python sandbox startup failure and correctly
remained unverified. After the sandbox fix, both prompt variants passed their
pilots.

The first complete 24-attempt development matrix exposed two measurement defects:

- Regression retention compared whole test-file hashes, incorrectly rejecting
  preserved regression tests when the model added boundary coverage. The corrected
  check preserves existing test ASTs and surrounding setup while allowing added
  methods; it also requires the previously failing test to execute successfully.
- The runtime parser treated reconnect warnings as fatal even when the CLI
  recovered and emitted a completed turn. Six attempts stopped for this reason,
  including all four pre-existing-failure attempts. The corrected parser accepts
  recognized reconnect warnings only with a successful completed turn; failures,
  malformed streams and missing completion still fail closed.

The development matrix is retained separately. The primary comparison reruns the
entire matrix after these corrections, not selected failing cells. Its scores do
not pool development attempts, and infrastructure interruptions are not attributed
to prompt quality.
