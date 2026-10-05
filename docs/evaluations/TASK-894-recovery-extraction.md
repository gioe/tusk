# TASK-894: Conditional recovery extraction

Baseline: commit `0e7de09bdfcec98e08b302040ec4130fb1e83307` (release 1279, after TASK-893 contract corrections).

This change moves exceptional instructions into flat companion files. It preserves workflow policy, including delegation, reproduction, verification, review, and completion requirements. It does not establish better model performance; TASK-895 owns behavioral comparisons.

## Before/after gate mapping

| Original area | Core after extraction | Conditional detail |
|---|---|---|
| Upgrade/reload and configuration | Retained, including exactly-once reload | None |
| Task identity, sessions, early exits, resume progress | Retained | None |
| Task brief, scope, context health, blocking questions, workflow routing | Retained | None |
| Workspace creation, writable-root checks, local wrapper/venv caveats | Retained | None |
| Deliverable convergence check | Run only when task-start requests it; read section before choosing recommendation | `deliverable-check`: all recommendation branches and their guards |
| Bug reproduction | Retained, including authoring missing regressions, meaningful test execution, visual evidence, time sensitivity, and isolated mutating reproductions | None |
| Exploration and implementation routing | Retained; lack of material progress triggers section before recovery | `stalled-exploration` and `stalled-implementation`: nudge, bounded replacement, fallback and no-interruption safeguards |
| Scope declaration and checkpoint expansion | Retained | None |
| Criterion commits, grouping, skipped/non-file work, follow-up dedup | Retained; deletion preflight reads section before a commit attempt | `commit-recovery`: concurrency, pathspec/symlink, formatter, deletion, attribution, timeout, failing tests, precheck verdicts, flake/rebase handling and bounded diagnosis |
| Progress checkpoint and schema migration reminder | Retained after commit recovery trigger | None |
| Local review, criteria completion, post-merge verification | Retained, including explicit deferral and recording later evidence | None |
| Merge-only lint and source release metadata | Retained, including bypass boundaries and release-before-review ordering | None |
| Configured review and unresolved-finding stop | Retained | None |
| Merge exceptions | Trigger before selecting fallback; exit-3 success-with-cleanup meaning retained | `merge-recovery`: already merged, divergence, branch/worktree/remote failures, cleanup and DB pinning where originally documented |
| PR/no-commit closure | Retained, including closure reasons, evidence and duplicate-session routing | None |
| Finalization | Retained: merge 0/3 or abandon 0 only, stable checkout, skill-run finish, canonical summary, retro | None |
| Other subcommands | Existing conditional reference retained | None |

Both companions use the same five stable anchors. Moved text is compared against each surface's own baseline, so pre-existing Claude/Codex differences are not silently rewritten in this refactor.

### Extraction audit at the baseline commit

All ten original blocks remain in their companion, compared with whitespace normalized (dedentation only). Baseline line spans below identify the immutable source for review:

| Anchor | Claude baseline lines | Codex baseline lines |
|---|---:|---:|
| deliverable-check | 159–171 | 329–388 |
| stalled-exploration | 207–223 | 478–494 |
| stalled-implementation | 256–270 | 558–572 |
| commit-recovery | 313–392 | 632–783 |
| merge-recovery | 485–511 | 910–949 |

## Distribution and reference resolution

Claude uses adjacent `RECOVERY.md`; Codex uses adjacent `tusk-recovery.md`. Each core prompt instructs the agent to load only the section whose condition occurred and resume at the triggering step unless routed elsewhere. All three installed companion paths, including the canonical compatibility copy, are registered in MANIFEST. Existing installer loops already copy flat skill files and Codex Markdown prompts; no installer behavior change is needed.

Existing generated agent-skill mirrors may lack newly introduced companion files. Canonical-generated mirrors first use the installed/source canonical companion, then the byte-identical compatibility copy distributed as codex-prompts/tusk-recovery-canonical.md and installed as .codex/prompts/tusk-recovery-canonical.md. Ordinary Codex prompts use only their own variant. The variants have pre-existing semantic differences (including deliverable checks and timeout/DB recovery), so cross-variant fallback would lose safeguards. An equality test enforces the compatibility copy's fidelity. These paths are retained across workspace cleanup. An absent reference stops the recovery branch instead of inviting a guessed bypass.

## Size measurements

Counts are whitespace-separated words / UTF-8 bytes. The initial required context means the core prompt alone; the companion is conditional.

| Core | Before words / bytes | After words / bytes | Companion words / bytes | Core word reduction |
|---|---:|---:|---:|---:|
| skills/tusk/SKILL.md | 9,960 / 68,576 | 7,112 / 49,192 | 3,270 / 22,371 | 28.6% |
| codex-prompts/tusk.md | 7,948 / 56,239 | 6,016 / 42,514 | 2,335 / 16,218 | 24.3% |

The Codex distribution additionally contains a 3,270-word / 22,371-byte exact canonical compatibility copy for existing generated skill mirrors. It is loaded only for a triggered recovery when the canonical companion is otherwise unavailable.

Combined core-plus-companion size may increase because routing instructions and section anchors are new. The claim is reduced initial loading, not reduced total text or measured dollar savings.

Reproduce from repository root:

```python
from pathlib import Path
import subprocess
base = "0e7de09bdfcec98e08b302040ec4130fb1e83307"
for core, companion in (
    ("skills/tusk/SKILL.md", "skills/tusk/RECOVERY.md"),
    ("codex-prompts/tusk.md", "codex-prompts/tusk-recovery.md"),
):
    before = subprocess.check_output(["git", "show", f"{base}:{core}"])
    after = Path(core).read_bytes()
    recovery = Path(companion).read_bytes()
    print(core, [(len(x.split()), len(x)) for x in (before, after, recovery)])
```

## Verification

- Resolve every conditional anchor from both source prompts and copies produced by the real upgrade copy functions.
- Check manifest ownership and the installed fallback after the actual generated-mirror text transformation.
- Keep core-gate tests against core files; move recovery assertions to their corresponding companion sections.
- Preserve TASK-893 lint and finalization assertions in the core; attribution assertions follow the moved examples.
- Run the focused guidance tests and the required full unit gate. These checks cover distribution and instruction contracts, not agent effectiveness.
