# TASK-251: Evaluation — /review-commits Step 5.1 Inline-vs-Agent Heuristic

## Problem Statement

`/review-commits` Step 5.1 chooses between an inline review (the orchestrator reads the diff and posts the verdict itself) and an agent review (a background `general-purpose` agent is spawned). The heuristic routes to the inline path when **any** of:

1. Diff is small (fewer than ~200 lines), OR
2. Diff contains only non-code files (`.md`, `.json`, `.yaml`), OR
3. `review.reviewer` is absent from `tusk/config.json` (no agent is configured).

Issue [#621](https://github.com/gioe/tusk/issues/621) introduced `tusk review begin` to bundle `review-diff-range` + `review start` into one CLI call, eliminating a documented zsh-quoting hazard. That issue **explicitly deferred** any change to the inline-vs-agent decision logic in Step 5.1, on the theory that the bundle changes the cost calculus (one CLI call instead of three) and may shift the breakpoint at which agent dispatch beats inline review.

This evaluation asks: does the `skill_runs` corpus support changing the heuristic now?

## Data

Snapshot from this project's tusk database (2026-04-30):

### Inline vs agent split

```
total reviews     : 225
inline reviews    : 225
agent reviews     : 0
```

**Zero historical reviews used the agent path.** The heuristic's clauses 1 and 2 (diff-size, file-type) have never been the deciding factor in this project; clause 3 (no reviewer configured) has fired 100% of the time:

```bash
$ tusk config review
{"mode":"ai_only","max_passes":2}
```

`review.reviewer` is absent — so every call to `/review-commits` automatically takes the inline path before the diff-size check is even consulted.

### Per-review cost data

`code_reviews.cost_dollars`, `tokens_in`, `tokens_out` columns exist in the schema but **no code path writes to them** (`grep -rn "code_reviews.*cost\|cost_dollars" bin/`):

```
SELECT COUNT(*) FROM code_reviews WHERE cost_dollars > 0;
=> 0
```

The only cost data available is at the `skill_runs` level, which mixes review work with subsequent must_fix fixes Claude implements.

### Whole-run skill cost (review + fixes)

```
                            runs   avg cost     avg tokens_in
all review-commits runs      185   $0.95       1.03M
runs that found issues        13   $2.40       2.49M
runs that approved            170  $0.85       0.94M
```

The 2.8x cost gap between approve-only and changes_requested runs is overwhelmingly the fix loop, not the review itself.

### Quality signal

14 of 225 reviews (~6.2%) returned `changes_requested`. Comment breakdown for those:

- `must_fix` flagged in 7 of 14 cases (correctly distinguishing critical from cosmetic)
- `suggest` is the most common category
- `defer` rarely used (≤2 per review)

Inline reviews demonstrably catch real issues at a reasonable rate.

## Findings

1. **The breakpoint cannot be re-evaluated from this corpus.** The agent path has never been exercised, so there is no comparison data. The "200 lines" threshold and the "non-code only" carve-out have never been the deciding factor — clause 3 short-circuits before either is checked.

2. **The bundle change in #621 affects shell-call count, not LLM cost.** `tusk review begin` saves 2 subprocess invocations and 1 jq parse. At the millisecond/sub-cent scale this is invisible against the dominant cost driver (LLM tokens spent reading the diff and producing the verdict). The hypothesis that the bundle "may shift the breakpoint" was a reasonable hedge at deferral time, but is not supported by the cost model.

3. **Per-review cost data is the missing input.** The `code_reviews` table has columns for `cost_dollars`, `tokens_in`, `tokens_out` but nothing populates them. Without that, even a project that does configure a reviewer agent and runs both paths cannot directly compare them — the skill_run-level numbers conflate review cost with fix-loop cost.

4. **Inline-path quality is acceptable today.** 6.2% changes_requested rate, with `must_fix` correctly applied in half of those, suggests the inline path is not silently rubber-stamping diffs.

## Recommendation

**Keep the Step 5.1 heuristic as-is.** Issue #621's deferral can be closed with no change to the decision logic. There is no evidence the bundle change shifted the breakpoint, and there is no data to validate the breakpoint itself.

The prerequisite for any future re-evaluation is **per-review cost instrumentation**: populate `code_reviews.cost_dollars`, `tokens_in`, `tokens_out` so that inline and agent paths can be compared on equal footing without the fix-loop noise. That is the concrete follow-up.

## Follow-up

- File a follow-up task: *Populate `code_reviews.cost_dollars/tokens_in/tokens_out` so per-review cost is observable separately from the whole skill_run.* This unblocks any future re-evaluation of Step 5.1 (or other review-strategy questions) by giving us a per-review cost signal that does not include downstream must_fix fix work.

- No change to the heuristic this round; no change to `/review-commits` SKILL.md.
