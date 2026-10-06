# Historical Tusk workflow replay pilot

TASK-896 extends the bounded TASK-895 action-protocol evaluation with actual
Codex shell tools, repository exploration, agent-authored tests, task-aware
commits, and a real isolated Tusk database. This pilot validates the harness on
two historical bugs; it is not evidence for changing production prompt policy.

## Comparison

The full variant uses `codex-prompts/tusk.md` from
`060b7b3e0ac6cc004f957252a3b765e4ab29bfe1` (release 1280). The compact candidate
is [compact.md](../../tests/evals/tusk_replay/compact.md). Both require delegated
exploration and retain regression-test, verification, scope, preservation, and
honest handoff requirements. This is a prompt-compression comparison, not another
mandatory-versus-conditional delegation comparison.

The core documents contain 6,016 and 272 whitespace-delimited words respectively.
Those counts exclude identical coordinator instructions and the shared recovery
companion, which is available on disk and is not eagerly injected. They do not
measure complete model context, billed tokens, or savings.

Both variants receive the same explicit phase boundary: bootstrap, task start,
and worktree creation are already complete. The agent explores, reproduces,
implements, tests, commits, reviews its diff, and records a handoff. Publication,
release metadata, merge, session closure, and retro are outside this pilot.
The task must remain In Progress with its original session open.

## Historical cases and independent proof

| Case | Starting revision | Known fixed revision | Calibration |
|---|---|---|---|
| TASK-886: false stale-path warnings from inline Python | `e438f50dd89f32841924ca45d54d2a89ef228eb2` | `418aca522b4749e47db8bbf29c0148496eb4c738` | 14/39 checks before; 39/39 after |
| TASK-892: nested Python caches block worktree cleanup | `dfb565054a116ebd9a3dac74afe0e1884646f749` | `2cee37dfe7c85a51317977aac5958d94debd9ed9` | 4/6 checks before; 6/6 after |

The path grader checks false warnings and retention of genuine missing shell
operands across interpreter and flag variants. It permits treating Python source
as opaque. The cache grader checks nested cleanup, preservation of notes,
similarly named directories and symlink targets, and actual Git worktree removal
or refusal. Neither grader relies on agent-authored tests to define correctness.
Each grader runs in a fresh Python process. Runtime errors and incomplete check
sets are infrastructure failures, not successful bug reproductions.

## Isolation

Each case is exported with `git archive` and initialized as a new repository,
without future objects or solution commits. Historical automatic agent settings
are removed. Each attempt has a fresh task record, database, session, task-owned
worktree, home directory, temporary directory, and local bare publication target.
Only sanitized original requirements are seeded; later task progress is absent.

Before model execution, the runner tests the same deny-by-default permission
profile used by tools. Live source, user and temporary authentication files,
hidden graders, and sibling artifacts must be unreadable. Writes outside the
execution tree and network connections must fail. Python runtime reads and
disposable Git metadata writes are explicitly permitted. Temporary credentials
belong to the authenticated CLI supervisor and are excluded from artifacts.
Post-run code execution and inspection remain sandboxed.

The fixture also adapts two historical `mktemp -t` calls in `bin/tusk` to pass
`-p "${TMPDIR:?}"`. BSD `mktemp -t` otherwise selects the host temporary directory
on this machine, which the sandbox correctly denies. The adaptation is committed
before the agent starts, is identical across variants, and is listed in the
manifest. It does not change the production CLI. Calibration includes an actual
restricted-shell `tusk task-get`, in addition to filesystem and network probes.

Removing historical agent settings invalidates unrelated prompt-presence tests,
so the shared commit gate runs the relevant existing suite for each case.
Agents must also run their new regression tests. Ordinary historical skills and
docs remain source files; trace audits must disclose competing workflow reads.

## Reproduction and interpretation

See the [runner README](../../tests/evals/tusk_replay/README.md) for requirements,
commands, output fields, and phase limitations. A full pilot is two cases × two
prompts × one repetition, using the same model and reasoning settings. Pair order
alternates between cases. Larger comparisons should add held-out cases and
repetitions before making policy decisions.

Inspect behavioral grades separately from workflow checks and manually audit
the executed regression failure, subsequent passing test, delegation, and final
claims. The automatic result does not prove every workflow requirement.
Parent-reported CLI usage does not establish child-inclusive totals; combined
tokens and cost remain unavailable rather than being inferred.

Named permissions follow the [official Codex permission documentation](https://learn.chatgpt.com/docs/permissions).
Trace-based checks follow [OpenAI's skill-evaluation guidance](https://developers.openai.com/blog/eval-skills).

## Pilot results

The final cohort uses `gpt-6-astra`, medium reasoning, a 1,200-second limit,
and one repetition per cell. The pinned runner, exact requests, raw traces,
patches, grader outputs, and manual audit are retained in
[the TASK-896 evidence directory](../../tests/evals/tusk_replay/artifacts/TASK-896/README.md).

| Case | Prompt | Frozen checks | Red before fix → final passing suite | Native delegation | Blind patch review | Agent elapsed |
|---|---|---|---|---|---|---|
| Inline Python | Full | 39/39 | 10 failures → 64 passes | Observed | Acceptable | 408.4 s |
| Inline Python | Compact | 39/39 | 10 failures → 58 unit + 7 integration passes | Observed | **Defect found** | 252.3 s |
| Nested caches | Compact | 6/6 | 3 failures → 14 passes | Observed | Acceptable | 443.8 s |
| Nested caches | Full | 6/6 | 3 failures → 20 passes | Observed | Acceptable | 458.3 s |

All four completed the audited test-before-fix workflow, made task-aware commits,
recorded verification evidence and criteria, and left the original task session
open. Existing tests were preserved. The final diffs received separate anonymous
review. The compact inline-Python run accurately reported its executed tests,
but its claim that all criteria were complete was too broad given the reproduced
defect; the manual workflow audit records that distinction. Thus the frozen automated score is 4/4, while the reviewed acceptable-patch
count is 3/4 (full 2/2, compact 1/2). The latter incorporates the post-hoc finding
below; it must not be presented as a pre-registered statistical comparison.

Elapsed time measures the agent subprocess, excluding setup and post-run grading.
These single observations are descriptive only. They do not establish latency
or cost savings. The full variants consulted the historical review companion;
the compact cache run read a portion of the shared recovery companion after
exploration errors. No competing core workflow read was observed. The full cache
run canceled its fixture skill run at handoff but retained the required open
task session.

### What the pilot already caught

The first diagnostic matrix could not use the historical CLI because BSD
`mktemp -t` selected a denied host temporary directory. A second diagnostic
cohort passed the inline-Python functional checks, but native child launches
failed because `--ephemeral` removed their required parent session context.
It was stopped after the completed pair; both unstarted cache cells remain
explicitly accounted for. Neither diagnostic cohort is pooled with the final
comparison. Their sources, traces, and failure accounting are preserved.

The final runner uses temporary persisted sessions and an isolated successful
child-launch probe. It exports only tool calls/results from session rollouts,
not credentials, system/developer messages, or reasoning. This supplementary
ledger is necessary because the CLI event stream omits native spawn calls.
Some tool arguments in the native ledger are encrypted by the runtime; child
launch outputs and separate child tool records still establish delegation.

Blind patch review used anonymous specimen names with original requirements
and baseline source. Reviewers did not receive variant labels, model results,
or traces. A reviewer found that the compact inline-Python patch stops scanning
at every long option. A supplemental sandbox probe confirmed that
`python3 --check-hash-based-pycs always -c 'print("missing/source.py")' missing/real.py`
incorrectly reports a source fragment as a path in that patch. The full patch
returns only `missing/real.py`. This is a post-hoc quality finding, not an extra
pre-registered test: the frozen scores remain unchanged. The next grader version
should include this case before another comparison.

### Decision and limits

Keep the production workflow unchanged. The compact candidate has a reproduced
acceptance gap despite passing the frozen checks. One observation cannot prove
that prompt compression caused it, or that the full prompt is generally better.
The useful result is that real replay plus independent review detects failures
that successful test counts alone miss.

Before a policy decision, add held-out tasks across multi-file changes, recovery,
user-file preservation, and handoff; freeze improved graders; then run repeated
paired trials. Include failures in the denominator and require both behavioral
and workflow evidence. Measure child-inclusive usage before claiming cost or
token savings. Separate shipment/recovery evaluations are still needed because
this pilot deliberately stops before release, merge, closure, and retro.

The harness has 27 focused unit tests. The task also refreshed three missing
recovery-file entries in the generated `.claude/tusk-manifest.json`, which had
blocked existing merge tests; no production prompt or CLI behavior changed.
