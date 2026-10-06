# TASK-896 replay evidence

Read the [evaluation report](../../../../../docs/evaluations/tusk-historical-replay.md)
for conclusions and limits. `index.json` records SHA-256 hashes and sizes.

| Bundle | Purpose | Included in final comparison? |
|---|---|---|
| `final-pilot.tar.gz` | Complete two-case, two-variant final cohort | Yes |
| `temporary-storage-diagnostic.tar.gz` | First matrix failed historical BSD temporary-file startup | No |
| `ephemeral-session-diagnostic.tar.gz` | Completed inline-Python pair with unavailable delegation; two explicitly unstarted cells | No |
| `delegation-probe.tar.gz` | Successful native child capability check with temporary persisted sessions | No |

`final-manifest.json`, `final-results.json`, and `final-summary.json` are readable
copies from the final bundle. Their automatic `correct` fields cover the frozen
behavioral checks and basic workflow state only. **They are not the overall
quality verdict:** blind review found a reproducible long-option defect in the
compact inline-Python patch despite its passing frozen score.

`workflow-audit.json` cites one-based raw CLI trace lines/item IDs and zero-based
native tool ledger records. It records observed failing regressions before
production edits, passing reruns, delegation, task-aware commits, and open-task
handoff. `blind-review.json` maps anonymous specimens to variants after review.
Reviewers saw only the case requirements, baseline source and anonymous diff;
they did not see model scores, labels, or traces. Supplemental long-option
reproduction commands and outputs are in the final bundle and were added after
blind review, without altering the original grader or score.

Bundles retain exact source files, prompts, manifests, raw model CLI traces,
filtered native tool ledgers, patches, grades and sandbox probes. They omit the
large repeated `execution/` trees, temporary authentication homes and bytecode
caches. No system/developer session messages or reasoning records are exported.
The native runtime encrypts some tool arguments; successful spawn outputs and
separate child records remain visible. Do not infer child-inclusive usage from
parent token counters. Combined tokens and cost are explicitly unavailable.

Inspect without extracting:

```bash
shasum -a 256 final-pilot.tar.gz
tar -tzf final-pilot.tar.gz
```

Extract only into a fresh disposable directory. Bundles contain evidence, not a
ready-to-run workspace. Reconstructing a patch requires the pinned historical
revision and common fixture adaptations from its manifest, then its final diff.
The runner README provides the command for a new independent replay; model
outputs and timings are not deterministic. There is no offline-rescoring CLI.
