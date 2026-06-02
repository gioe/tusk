---
name: address-issue
description: Fetch a GitHub issue, create a tusk task from it, and work through it with /tusk
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

# Address Issue Skill

Fetches a GitHub issue, converts it into a tusk task, and immediately begins working on it using the full `/tusk` workflow.

## Step 1: Parse the Issue Reference

Invoked with an optional issue number, full URL, or cluster selector:
- `/address-issue 314`
- `/address-issue https://github.com/gioe/tusk/issues/314`
- `/address-issue --cluster worktree` (select one issue labeled `cluster:worktree`)
- `/address-issue --cluster worktree --batch`
- no argument to default to the newest open issue

Parse flags first:
- `--cluster <name>` sets `$CLUSTER`. Any `cluster:<name>` label currently present on the repo is accepted — the skill does not validate against a closed list, so new clusters added to GitHub work immediately without a skill edit. Run `gh label list --repo gioe/tusk --search "cluster:"` to see the current set if you're unsure which clusters exist.
- `--batch` is valid only with `--cluster`.
- A number or full URL must not be combined with `--cluster`; if both are present, stop and ask the user to choose one mode.

Extract the issue number:
- Full URL → parse the number from the path.
- Number only → use it directly.
- `--cluster <name>` without `--batch` → fetch open issues with that cluster label, then choose the highest-leverage issue:
  ```bash
  CLUSTER="<name>"
  gh issue list --repo gioe/tusk --state open --label "cluster:$CLUSTER" --limit 50 --json number,title,labels,updatedAt,url
  ```
  Prefer the broadest canonical/root-cause issue in the cluster, especially one whose title names an underlying subsystem or behavior rather than a one-off symptom. Avoid issues labeled `duplicate`, `invalid`, or `wontfix`. If several issues look equivalent, choose the most recently updated issue. Display the selected issue and continue with Step 2:
  > Cluster `cluster:<name>` — selected highest-leverage issue #<number> "<title>" from <count> open issue(s).
- No argument → fetch the newest open issue:
  ```bash
  gh issue list --repo gioe/tusk --state open --limit 1 --json number,title
  ```
  If empty, report `> No open issues found in gioe/tusk.` and stop. Otherwise use the returned `number` and display: `> No issue specified — defaulting to newest open issue: #<number> "<title>"`

### Batch Cluster Mode

When invoked as `/address-issue --cluster worktree --batch`, do **not** treat the whole cluster as one task and do **not** create one task per GitHub issue. Batch mode is a cluster grooming and execution pass: create one tusk task per root cause, not one task per GitHub issue.

1. Fetch every open issue in the cluster:
   ```bash
   CLUSTER="<name>"
   gh issue list --repo gioe/tusk --state open --label "cluster:$CLUSTER" --limit 100 --json number,title,labels,updatedAt,url
   ```

2. Fetch bodies and comments for each issue with `gh issue view <number> --repo gioe/tusk --json number,title,body,labels,comments,state,url`.

3. Group issues by root cause. Treat reports as the same group when they name the same command, same failure mode, and same likely fix. Keep separate groups when they share a cluster but differ in command surface or acceptance criteria.

4. Present a table before creating tasks:

   | Group | Canonical issue | Covered issues | Root cause | Proposed task summary |
   |-------|-----------------|----------------|------------|-----------------------|

   The canonical issue should be the clearest, broadest report. The covered issues list must include every GitHub issue number in the group.

5. Ask for approval:

   > Create one tusk task per root-cause group and process them sequentially? (**confirm** / edit / cancel)

   On `edit`, update the grouping and show the table again. On `cancel`, stop.

6. **Per-group loop.** For each approved group, run Steps 2-9 using the canonical issue as the primary issue. In Step 4's task description, append:

   ```markdown
   ## Covered GitHub Issues

   - #<canonical> — <url>
   - #<covered> — <url>
   ```

   In Step 9, close every covered issue after the task is merged. For the canonical issue, use the normal resolution comment. For non-canonical covered issues, use:

   ```bash
   gh issue close <covered_number> --repo <owner/repo> --comment "Resolved by the same root-cause fix as #<canonical> in <commit_sha>. Tracked as tusk task #<task_id>."
   ```

   Apply Shared gh Failure Handling to every close/comment call.

   **Run Step 10's per-group sub-steps inline.** After Step 9 closes the issue(s), close the /tusk skill-run with `tusk skill-run finish <run_id>` and print the per-task rollup with `tusk task-summary <task_id> --format markdown` so each task gets its identity/cost/duration/diff/criteria block before the next group starts. **Do NOT invoke `/retro <task_id>` per group** — retro is deferred to Step 7 below.

   Continue to the next root-cause group only after the per-group sub-steps above complete for the current group. Accumulate every merged task ID into a `BATCH_TASK_IDS` list as you go — Step 7 reads it.

7. **End-of-batch consolidated retro (issue #832).** After every approved group has completed Steps 2-9 plus the per-group portion of Step 10, run `/retro` exactly **once** for the entire batch session — not once per group. Per-group `/retro` is intentionally redundant: each retro re-fetches config + backlog + retro-themes and re-analyses the same growing conversation tail, producing partial overlap with earlier retros and N skill-runs for N groups. One consolidated retro covering all merged task IDs is cheaper and produces fewer redundant findings.

   Pass the most recently merged task ID as the retro's `<task_id>` argument so cost attribution lands on a real task (issue #805), and explicitly name every merged task ID in the surrounding conversation so the retro analysis covers the full batch:

   ```
   Read file: <base_directory>/../retro/SKILL.md
   ```

   Hand off to /retro with a short preface like:

   > Batch session covered tusk tasks: TASK-<id1>, TASK-<id2>, …, TASK-<idN>. Running consolidated retro keyed to TASK-<idN>.

   This is the **batch-mode override of Step 10's `/retro <task_id>` invocation**. Single-issue (non-batch) invocations of `/address-issue` still run `/retro` exactly once at Step 10 below as usual.

## Step 2: Fetch the Issue

Use `gh` to fetch the issue. Detect the repo from the argument:
- If a full URL was given, extract `owner/repo` from it.
- If only a number was given, default to `gioe/tusk`.

```bash
gh issue view <number> --repo <owner/repo> --json number,title,body,labels,comments,state
```

If the issue is already closed (`state: "CLOSED"`), warn the user:

> Issue #<N> is already closed. Do you still want to create a task for it?

Wait for confirmation before proceeding.

## Step 3: Fetch Config and Backlog

```bash
tusk setup
```

Store the `config` (domains, task_types, agents, priorities, complexity) and `backlog` (for duplicate detection).

## Step 4: Analyze the Issue and Determine Task Fields

Using the issue `title`, `body`, and `labels`, determine:

| Field | How to Determine |
|-------|-----------------|
| **summary** | Derive from the issue title — keep it imperative and under ~100 chars. Prefix with "Fix:" for bugs, otherwise use the title as-is or rephrase as an action. |
| **description** | Include the full issue body as context, plus the issue URL as a reference link. Format: `GitHub Issue #<N>: <url>\n\n<body>` |
| **priority** | Infer from labels: `priority: high` / `critical` / `urgent` → `High`/`Highest`; `priority: low` → `Low`; labels like `bug` or `regression` → lean `High`; default `Medium`. |
| **domain** | Match the issue's subject area to a configured domain. Leave NULL if no match. |
| **task_type** | `bug` for issues labeled `bug` or `defect`; `feature` for `enhancement`/`feature request`; `docs` for `documentation`; otherwise `feature`. |
| **assignee** | Match to a configured agent if the domain/labels clearly indicate one. Leave NULL if unsure. |
| **complexity** | Estimate from the issue body length and scope. Short reproduction steps with a clear fix → `S`; broad feature request → `M`; major architectural change → `L`. |

Generate **3–7 acceptance criteria** from the issue body — concrete, testable conditions. For bug issues, always include a criterion that the failure case is resolved and a regression test criterion.

### Failing Test Polarity Convention

Every spec stored as a typed-criteria `test` (Step 4.1 → Step 6) must satisfy two invariants — Step 4.1 checks the first, Step 7.5 checks the second:

1. **Exits nonzero against the broken codebase** (sandbox-validated at Step 4.1, when feasible).
2. **Exits 0 against the fixed codebase** (re-run authoritatively at Step 7.5, post-implementation).

Assertion-style specs — `test -z "$(...)"`, `test ! -e ...`, leading `!`, or any "negate the expected output" pattern — silently invert the polarity: they exit nonzero on broken AND fixed code, just for different reasons. Step 4.1 reads the broken-state nonzero as "fails as expected" and stores the spec verbatim, then `tusk criteria done` blocks merge indefinitely because the same spec still exits nonzero against the fix. Step 7.5 catches that mismatch authoritatively (issue #642, original incident TASK-287 / criterion #1291).

If you must write an assertion-style reproducer, wrap it: `! ( test -z "$(...)" )` so the fixed-code state exits 0. The issue template's `failing_test` field describes the same convention from the author's side.

## Step 4.1: Extract Failing Test Criterion

Scan the issue body for a `## Failing Test` section. If present:

1. **Extract the spec.** Prefer the **first** fenced block after the heading (triple- or single-backtick, with optional language tag); trim surrounding whitespace.

   **Plain-text fallback — if no fenced block is found**, treat the plain text between the `## Failing Test` heading and the next heading (or end of body) as the spec. Drop `#`-prefixed lines (shell comments) and trim whitespace. If non-empty, use as `<test_spec>` (sandbox flow in item 2 applies identically). If empty, fall through to item 3.

2. **Validate and classify the spec via `tusk address-issue classify-spec`.** The spec is arbitrary shell code from a GitHub issue body and must be treated as untrusted. The `classify-spec` helper centralises five chunks of logic (effective first-token resolution with `bash`/`sh -c` wrapper peel; issue #589 short-circuit for `/`-containing tokens; `command -v` PATH check on the sandbox PATH `/usr/bin:/bin`; post-sandbox malformed/environmental/interpreter-wrapper-bypass routing; the recommended downstream `action`).

   **a. Pre-flight call — is sandbox needed?**

   ```bash
   PREFLIGHT=$(printf '%s' "$TEST_SPEC" | tusk address-issue classify-spec)
   PREFLIGHT_EXIT=$?
   ```

   - **`PREFLIGHT_EXIT == 0`** — the helper classified the spec without needing a sandbox (the effective first token is off `/usr/bin:/bin`, so the sandbox would exit 127 anyway — the documented Step 4.1.a fast-path skip, including the issue #589 `/`-containing-token short-circuit). Parse `$PREFLIGHT` for `action`/`test_present`/`reason`/`effective_first_token` and **skip directly to item 2.d** (act on the result). Surface the helper's `reason` to the user as a one-line note.
   - **`PREFLIGHT_EXIT == 2`** — the effective first token resolves on the sandbox PATH; the helper cannot classify without sandbox results. Continue to **item 2.b** (approval + sandbox).

   The helper handles the wrapper peel (`bash -c '<body>'`, `sh -c '<body>'`) and the `/`-containing-token short-circuit internally, so the orchestrator never has to reimplement them.

   **b. Display the spec and request approval:**

   > The issue body's `## Failing Test` section contains this spec. If approved, it runs in an isolated sandbox (`env -i`, `PATH=/usr/bin:/bin`, no `.git` parent) — project tools like `tusk`, `pytest`, and any project-installed binary are off PATH. The sandbox confirms the spec is *runnable and exits nonzero on broken state*; the authoritative "does it actually fail on the current code" check happens later via `tusk criteria done`.
   > ```
   > <test_spec>
   > ```
   > **Options:** `run` (execute in sandbox), `skip` (do not execute — treat as `test_spec = null`).

   Treat anything other than an explicit `run` as `skip`. On skip, set `test_spec = null` and score `test_present = "unverifiable"` — the user-typed skip path is epistemically the same as the fast-path skip (the `## Failing Test` section was syntactically present but unvalidated). Do **not** route to item 3 — that path is reserved for the section-absent case (`test_present="no"`).

   **c. On approval, execute the spec in an isolated sandbox:**

   ```bash
   SANDBOX_DIR=$(mktemp -d)
   (
     cd "$SANDBOX_DIR" &&
     env -i HOME="$SANDBOX_DIR" PATH="/usr/bin:/bin" \
       bash -c "$TEST_SPEC" 2>"$SANDBOX_DIR/stderr.txt"
   )
   SPEC_EXIT=$?
   SPEC_STDERR_FILE="$SANDBOX_DIR/stderr.txt"
   ```

   **Why each layer matters — preserve all three when editing this step:**
   - `cd "$SANDBOX_DIR"` — `tusk` and `git` both walk up from `$PWD` to find a repo root. A throwaway tempdir has no `.git`, so the walk-up terminates inside the sandbox rather than discovering the host repo. Without this, a spec that calls `tusk commit` or `git` from the tusk source repo's cwd would execute against the real repo (observed in TASK-93).
   - `env -i` — drops inherited environment (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `TUSK_DB`, shell customizations) so the spec cannot read secrets or redirect writes to a different database via `TUSK_DB`.
   - `PATH="/usr/bin:/bin"` — keeps project-installed tools off the search path. The classify-spec helper resolves on-PATH against this exact value, so the helper's classification matches what the sandbox itself sees.

   Then re-invoke the helper with the sandbox results:

   ```bash
   RESULT=$(printf '%s' "$TEST_SPEC" | tusk address-issue classify-spec \
       --sandbox-exit "$SPEC_EXIT" \
       --sandbox-stderr-file "$SPEC_STDERR_FILE")
   rm -rf "$SANDBOX_DIR"
   ```

   For the **exit-zero** case (`SPEC_EXIT == 0`), prompt the implementer: discard (`test_present="no"`) or keep-with-warning (`test_present="unverifiable"` — the spec was attempted but didn't reach validation logic, equivalent in epistemic value to a user-typed skip; preserves the invariant that `test_present="yes"` means the bug was observed to fail under our own execution). Pass the choice via `--exit-zero-decision keep|discard` and re-invoke. If kept, Step 7.5 will re-run the spec against the fixed code post-implementation and surface a polarity-mismatch warning if it then exits nonzero (see the Failing Test Polarity Convention above).

   **d. Act on the helper's returned tuple.**

   The helper emits a single-line JSON object: `{action, test_present, reason, effective_first_token, on_path}`. The `action` field directs the downstream flow:

   | `action` | `test_present` | What to do |
   |---|---|---|
   | `"store"` | `"yes"` | Store as `test_spec` and proceed. **Polarity caveat:** the sandbox only confirms the spec exits nonzero on the *broken* state — it cannot tell whether the polarity is correct (exit 0 ≡ pass after fix) or inverted. Step 7.5 catches the latter authoritatively. Before storing, verify the spec calls into the project under test — self-contained specs with inline logic may exit nonzero yet pass trivially once that inline logic is fixed; surface this in Step 7 so the implementer validates manually. |
   | `"null"` | `"unverifiable"` | Set `test_spec = null`, do not add a test criterion in Step 6, surface the helper's `reason` field as a one-line note. The `"unverifiable"` score sits between `"yes"` and `"no"` (`config.default.json` `issue_scoring.factors.test_present`). |
   | `"discard"` | `"no"` | Set `test_spec = null`, do not add a test criterion in Step 6, surface the helper's `reason` field. Treat as if no `## Failing Test` section was supplied. |

   The helper's `reason` field names the deciding signal (exit code + stderr substring + missing token). Adding a new interpreter or text-tool signature is a one-line change in `bin/tusk-address-issue.py` (extend `_extract_wrapper_match` or `TEXT_TOOLS`) plus a unit test in `tests/unit/test_classify_spec.py` — no SKILL.md prose change required.

3. **If no `## Failing Test` section is found**, set `test_spec = null`. No test criterion is added in Step 6. For `bug`/`defect` task types, this lowers the Step 4.7 score via `test_present`; for other task types, `test_present` is N/A.

## Step 4.5: Optional Codebase Investigation

**Skip if complexity is XS or S.** Only run for M, L, or XL.

Ask the user:

> Before presenting the proposal, should I investigate the codebase for context? (**yes** / **no**, default: no)

Treat any non-`yes` response as skip. On **yes**:

1. **Read-only investigation.** Tools: `Read`, `Grep`, `Glob`, and read-only `Bash` (tusk CLI queries, `ls`, directory inspection — no writes, no edits, no commits). Cap at ~10 tool calls; summarize even if incomplete. Look for:
   - Files/functions tied to the issue's subject (search by keyword, class, config key)
   - Existing tests for the affected paths
   - Established conventions for similar features
   - Any partial implementation already present
   - Related tusk tasks: `tusk task-list --format json | jq '.[] | select(.summary | ascii_downcase | contains("<keyword>"))'`

   **Read entry points, not just helpers.** When inspecting a file that defines a `main()` / `if __name__ == "__main__":` block (or an analogous orchestrator/dispatcher), you must also read those entry points — not just helper definitions. A helper that looks unused in isolation may be invoked downstream by the orchestrator. Concluding "X not implemented" purely from helper reads — without checking the call sites — is the failure mode that produced TASK-276 (issue #637): `regen_triggers` was defined at line 98 of `bin/tusk-migrate.py` and looked unused, but the call site at lines 2628–2634 inside `main()` invoked it as the final step of every migrate run.

2. **Summarize** findings as a short bullet list before proceeding.

3. **Refine Step 4 fields**: sharpen `description` (name files/functions), tighten criteria to match real code structure, adjust `complexity` if warranted. Do **not** change `summary`, `priority`, or `domain` unless the investigation reveals a fundamental misclassification.

## Step 4.6: Already-Resolved Check (all task types)

**Always run this step.** The exact question depends on `task_type`:

- **`bug` / `defect`** — confirm the failure is still reproducible against the current code.
- **All other task types** (`feature`, `refactor`, `docs`, etc.) — confirm the implementation is not already shipped. Reframe the proposal as the question "is this already wired up on `main`?" before grepping or reading.

Use at most **3 tool calls** total (Grep, Read, or Bash read-only) regardless of task type. **Prefer invoking the affected code path directly** (e.g. running the actual command with a known input) over grepping for static markers — a live invocation surfaces regex bugs, off-by-one errors, and silent failures that grep-and-read miss. **When reading a source file that defines a `main()` / `if __name__ == "__main__":` block (or an analogous orchestrator/dispatcher), also read those entry points — not just helper definitions.** A helper that looks unwired in isolation is often invoked downstream by the orchestrator; concluding "X not implemented" from helper reads alone is the same failure mode that produced TASK-276 (issue #637). When the budget is tight, spend a tool call on `grep -n '<helper_name>' <file>` to locate every call site before reading.

**Sandbox state-mutating reproductions — tusk-on-tusk hazard.** "Invoking the affected code path directly" above means *read-only* invocation by default — running the live command with a known input (e.g. `tusk task-list`, `tusk config`, `--help` flags, `tusk task-get <id>`) is what surfaces the regex/dispatcher/silent-failure bugs grep alone misses; that remains the recommended default. The hazard is when the only way to demonstrate the issue is a state-mutating command (`tusk task-insert`, `tusk task-update`, `tusk review approve|request-changes|add-comment`, `tusk criteria done`, `tusk merge`, `tusk task-done`) and the affected code path IS `tusk` itself — invoking it directly mutates the orchestrator's live database, the same hazard Step 4.1's sandbox prevents. Concrete incident: TASK-209's post-fix `bin/tusk review request-changes 1 --note "test rationale"` repro overwrote review #1 (a stale March review on a long-closed task). For write-mutating reproductions, in order of preference: (1) prefer `--dry-run` if the tool offers it; (2) copy the live DB and pin tusk to the copy (`cp "$(tusk path)" /tmp/tusk-throwaway.db && TUSK_DB=/tmp/tusk-throwaway.db tusk <cmd>`) so writes land in the throwaway — `TUSK_DB` pins only the DB path, so `tusk` stays on PATH and the repo-root walk-up still works; (3) defer the live check to `tusk criteria done` after task creation, where the spec runs as part of the implementation cycle. Do **not** invoke state-mutating tusk subcommands directly against the orchestrator's DB during this step.

**Don't trust local source files when origin may be ahead.** Before reading source to confirm a bug is still present, spend one of the 3 tool calls on `git log --grep="TASK-" -n 20 -- <affected_file>` to see whether any recent in-flight task touched the area. If recent TASK-tagged commits exist, also run `git fetch origin <default> 2>/dev/null && git log <default>..origin/<default> --oneline | head` to confirm local default branch isn't stale relative to origin — a fresh fetch may already carry the fix. Original incident: TASK-419 (issue #833) — the fix had shipped on origin/main via TASK-412 ~1.5h before #833 was filed by an instance running an older tusk version; the Step 4.6 grep read the stale local source and confirmed the bug "still exists," proceeding to create a duplicate task that had to be abandoned. The reporter-side staleness is one layer (the filer's tusk version was behind); this guard catches the orchestrator-side staleness (your local checkout is behind what already shipped).

**Staleness recovery — one-liner.** When the staleness check above detects local default branch behind origin, run `tusk sync-main` instead of doing the four-step manual recovery (stash, fetch, ff-pull, stash pop, migrate) by hand. The helper resolves the default branch, fetches it, stashes by unique-name reference (mirroring `tusk test-precheck`'s pattern so concurrent invocations cannot collide and a pop never lands on a stale unrelated entry), fast-forwards via `git merge --ff-only origin/<default>`, pops the stash by ref, and runs `tusk migrate` to apply any schema migrations the new commits brought in. Emits a single JSON object: `{success, default_branch, fetched_commits, stashed, migrated}` — exit 0 on success, exit 1 on a recoverable failure (stderr names the failed step). If the helper exits non-zero with a diverged-branch hint (the ff-only merge refused), surface the message verbatim — local commits cannot be auto-rebased through this path.

If you find clear evidence the issue is already addressed (the bug is fixed, the proposed feature is already shipped and wired up, or the code path described no longer exists), surface this before proceeding:

> **Already-resolved note:** This issue may already be addressed — [brief explanation]. Do you still want to create a task?

Wait for user confirmation before proceeding to Step 5. If the issue is confirmed still present, or if you cannot determine either way within 3 calls, proceed without comment.

## Step 4.7: Model Recommendation (Config-Driven Scoring)

Read `issue_scoring` from the config fetched in Step 3:

```
scoring    = config["issue_scoring"]
factors    = scoring["factors"]
thresholds = scoring["thresholds"]
```

Evaluate each factor and look up its score contribution from `factors`:

| Factor key | Condition to evaluate | Value key |
|---|---|---|
| `test_present` | Resolve from Step 4.1's outcome via the **Step 4.7.1 resolution table** below. **Only evaluate for `bug` and `defect` task types.** For all other task types (`docs`, `feature`, `refactor`, etc.), treat as N/A: contribution = 0 regardless of value. | `"yes"` / `"no"` / `"unverifiable"` |
| `pillar_aligned` | Does the issue align with the project pillars (run `tusk pillars list` to fetch `[{id, name, core_claim}]`)? If the list is empty, skip (contribution = 0). | `"yes"` / `"no"` |
| `duplicate` | Is an open task already covering this issue (from Step 3 backlog)? Include the task ID in the rationale if yes. | `"yes"` / `"no"` |
| `in_scope` | Does the issue fit the project's stated purpose? | `"yes"` / `"no"` |
| `severity_high` | Does inaction risk data loss, user-facing breakage, or a security vulnerability? | `"yes"` / `"no"` |
| `issue_quality` | Is the report clear, reproducible, and actionable? | `"good"` / `"poor"` |

For each factor: `contribution = factors[factor_key][value_key]`

Compute: `total = sum of all factor contributions`

Assign verdict from thresholds:
- `total >= thresholds["address"]` → **Address**
- `total <= thresholds["decline"]` → **Decline**
- Otherwise → **Address** (borderline — still create and work the task; the score breakdown surfaces the uncertainty for the user)

Record the verdict, per-factor contributions, total, and a 1–2 sentence rationale for display in Step 5.

### Step 4.7.1: `test_present` resolution table

For `bug` and `defect` task types, resolve the `test_present` value scored by Step 4.7's factor row using the table below. Each row corresponds to one Step 4.1 outcome. The first matching row wins.

| # | Sandbox outcome | Stderr signature | `test_present` | Rationale |
|---|---|---|---|---|
| 1 | `## Failing Test` section absent | — | `"no"` | No reproducer was supplied. |
| 2 | Section present, **not** sandbox-executed — Step 4.1.a fast-path skip (effective first token off-PATH) | — | `"unverifiable"` | Author supplied a concrete reproducer but it can't be validated under the sandbox's safety constraints. |
| 3 | Section present, **not** sandbox-executed — Step 4.1.b user-typed skip | — | `"unverifiable"` | Author supplied a concrete reproducer but the user declined sandbox validation; same epistemic situation as the fast-path skip. |
| 4 | Section present, sandbox-executed, **exit ≠ 0** | No command-error signature (none of the rows below match) | `"yes"` | Bug observed to fail under our own execution — the canonical positive case; `"yes"` means we observed the bug fail. |
| 5 | Section present, sandbox-executed, **exit 0**, implementer chose `keep` (Step 4.1.c) | Any (irrelevant) | `"unverifiable"` | The spec's self-skip guard fired in the sandbox tempdir (typically `git diff` against a missing `.git` parent); the spec was attempted but didn't reach validation logic — equivalent in epistemic value to a user-typed skip. Preserves the invariant that `"yes"` means we observed the bug fail under our own execution. |
| 6 | Section present, sandbox-executed, **exit 0**, implementer chose `discard` (Step 4.1.c) | Any (irrelevant) | `"no"` | Spec discarded as no-longer-failing; treat as if no `## Failing Test` section was supplied. |
| 7 | Section present, sandbox-executed, **command error — *malformed spec*** | Stderr contains `command not found` / `syntax error`, OR exit 126/127 with stderr matching neither the empty nor `No such file or directory` environmental signature | `"no"` | Spec was actually run and demonstrably malformed — distinct from the skip path because the spec was executed, not merely unsandboxable. |
| 8 | Section present, sandbox-executed, **command error — *environmental*** | Either: (a) exit 126 or 127 with stderr empty or containing `No such file or directory`, NOT `command not found` / `syntax error`; OR (b) exit 1 or 2 from a POSIX text utility (`grep`/`awk`/`sed`/`find`/`cat`/...) with a `<tool>: ... No such file or directory` stderr line — issue #659. The 1/2 case covers text tools that handle missing inputs internally rather than letting exec fail with 127. | `"unverifiable"` | Spec invokes a tool or relative path unreachable from the sandbox tempdir (typically a project-relative path like `bin/tusk`, `tests/...`, or files referenced by a text-tool command whose first token IS on PATH — so the Step 4.1.a fast-path didn't fire); same epistemic situation as the fast-path skip. |
| 9 | Section present, sandbox-executed, **command error — *interpreter-wrapper-bypass*** | Exit nonzero AND NOT 126/127; stderr contains a canonical missing-executable signature — Python's `FileNotFoundError: ... '<token>'`, Python `-m` form's `<python3 path>: No module named <token>` (skip the `command -v` PATH check — `<token>` is a Python module name, not an executable, and module reachability depends on Python site-packages which `env -i` strips), Node's `spawn <token> ENOENT`, Ruby's `Errno::ENOENT: ... <token>`, Perl's `Can't exec "<token>"`, or a generic `<token>: No such file or directory` — naming a token whose basename does not resolve on `/usr/bin:/bin` | `"unverifiable"` | The wrapper interpreter (`python3`, `node`, `ruby`, `perl`, etc.) ran cleanly on the sandbox PATH but the body's inner subprocess could not reach the project tool; same epistemic situation as the fast-path skip. |

## Step 5: Present Proposed Task for Review

Open with a **Model Recommendation** block (including the score breakdown from Step 4.7), then show the proposed task:

```markdown
### Model Recommendation

> **Recommendation: <Address / Decline>** — <1–2 sentence rationale from Step 4.7>
>
> **Score:** test_present: <±N>, pillar_aligned: <±N>, duplicate: <±N>, in_scope: <±N>, severity_high: <±N>, issue_quality: <±N> → **total: <N>** (Address ≥ <thresholds.address>, Decline ≤ <thresholds.decline>)

When `test_present` is `"unverifiable"`, suffix that contribution with the value key in the rendered Score line — e.g. `test_present: +1 (unverifiable)` — so readers can tell it apart from the binary `"yes"` (+2) and `"no"` (-1) cases. The other factors are binary and need no annotation.

## Proposed Task from Issue #<N>

**<summary>** (<priority> · <domain> · <task_type> · <complexity>)
> <description preview — first 2 sentences>

**Acceptance Criteria:**
1. <criterion 1>
2. <criterion 2>
...
```

Then ask the user to choose, **bolding the option that matches the Model Recommendation**. For a Decline recommendation, replace "confirm" with "proceed anyway" in the prompt:

> Create this task? You can confirm (implement now), edit (e.g., "change priority to High"), decline (close the issue without creating a task), or cancel.

The user retains full veto power — any option may be chosen regardless of the recommendation. Wait for explicit approval before inserting.

### Shared gh Failure Handling

Referenced by the Decline Path and Step 9. When a `gh issue close` or `gh issue comment` call fails:

1. If the error contains `already in a 'closed'` state, retry the action as `gh issue comment <number> --repo <owner/repo> --body "<same body>"`.
2. If the retry also fails, or the original error was something else (permissions, locked issue, etc.), surface the manual URL and the message to paste:
   > Could not update issue #<N> automatically. Please visit https://github.com/<owner/repo>/issues/<N> and add this comment: "<body>"

Never abort the skill on a gh failure — continue the flow with the manual-URL fallback.

### Decline Path

If the user types **decline** (optionally followed by an inline rationale, e.g. `decline out of scope`):

1. If no rationale was given, prompt the user to pick one: `out of scope`, `won't fix`, `already handled by TASK-<id>`, `duplicate of #<issue>`, or a free-text reason.

2. Close the issue:
   ```bash
   gh issue close <number> --repo <owner/repo> --comment "Declined: <rationale>"
   ```
   - Success → > **Declined** — Issue #<N> closed. Reason: <rationale>. No task created.
   - Failure → apply **Shared gh Failure Handling**; on the already-closed retry path, the summary becomes: > Issue #<N> is already closed. Reason recorded: <rationale>. No task created.

3. **Do NOT insert a task.** Stop — do not proceed to Step 6.

## Step 6: Deduplicate and Insert

Check for semantic duplicates against the backlog from Step 3. If a likely duplicate exists, surface it:

> Possible duplicate: existing task #<id> — "<summary>". Proceed anyway?

If confirmed (or no duplicate found), write the full task description to a
temporary UTF-8 file first, then insert with `--description-file`. The issue
body is untrusted text from GitHub and may contain shell metacharacters such as
`$0`, `$SHELL`, backticks, or `$(...)`; do not pass it as an interpolated shell
argument. Use the Write tool or another non-interpolating file write so the
file contents are exactly:

```text
GitHub Issue #<N>: <url>

<body>
```

Then run:

```bash
tusk task-insert "<summary>" \
  --description-file "<description_file>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --assignee "<assignee>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --criteria "<criterion 2>" \
  --criteria "<criterion 3>"
```

Omit `--domain` and `--assignee` if NULL. Do not pass empty strings.

**If `test_spec` is set (from Step 4.1)**, append one additional `--typed-criteria` argument to the insert command:

```bash
  --typed-criteria '{"text":"Failing test passes","type":"test","spec":"<test_spec>"}'
```

Replace `<test_spec>` with the extracted command verbatim.

**Single-quote escaping:** If `test_spec` contains a single quote (e.g., a pytest selector like `tests/test_foo.py::test_it's_broken`), the single-quoted JSON wrapper above will break. In that case, assign the spec to a shell variable and use double-quoted outer JSON with escaped inner quotes:

```bash
TEST_SPEC='tests/test_foo.py::test_it'"'"'s_broken'   # use '"'"' to embed a literal single quote
  --typed-criteria "{\"text\":\"Failing test passes\",\"type\":\"test\",\"spec\":\"$TEST_SPEC\"}"
```

The variable form is safe for specs that contain neither `"` nor `\`.

**Specs containing `"` or `\` (or both): use `tusk typed-criteria-build`.** Test specs lifted verbatim from GitHub issue bodies routinely mix single quotes, double quotes, and backslashes — for example, a heredoc reproducer like `printf %s "$JSON" | python3 -c "import json,sys; json.load(sys.stdin)"`. Both shell-quoting forms above silently produce malformed JSON in that case (issue #639). Pipe the spec through the helper instead — it lets Python's `json.dumps` handle every escape, then you embed the result via plain `$(...)` substitution:

```bash
JSON=$(printf '%s' "$TEST_SPEC" | tusk typed-criteria-build)
  --typed-criteria "$JSON"
```

Or, when the spec lives in a file (e.g. you wrote it to a tempfile during Step 4.1 sandbox validation):

```bash
JSON=$(tusk typed-criteria-build --spec-file /tmp/spec)
  --typed-criteria "$JSON"
```

`tusk typed-criteria-build` defaults to `text="Failing test passes"` and `type="test"`; pass `--text <text>` / `--type <type>` to override. Use this helper whenever the spec contains `"`, `\`, or any character whose shell escape isn't obvious — it removes the failure mode entirely rather than asking each caller to reinvent the escape.

This criterion will be validated by running the spec as a shell command when `tusk criteria done <cid>` is called — it blocks closure if the command exits nonzero.

**Exit code 0** — success. Note the `task_id` from the JSON output.

**Exit code 1** — heuristic duplicate found. Report the matched task and stop:

> Skipped — duplicate of existing task #<id> (similarity <score>).

Then branch on the duplicate task's current status before handing off:

- **To Do duplicate** — run `/tusk <id>` to start normal work on the existing task.
- **In Progress duplicate** — run `/resume-task <id>` instead of `/tusk <id>`, or explicitly reuse the existing open session and open skill-run if you have already fetched them. Do not start a fresh `/tusk <id>` run for an In Progress duplicate: `tusk task-start <id> --force --skill tusk` opens a new skill-run row and can orphan the prior open skill-run.
- **Done duplicate** — do not create or resume work. Surface the completed task as the resolution path.

**Exit code 2** — error. Report and stop.

## Step 7: Begin Work (Steps 1–11 Only)

**Dirty checkout guard.** Before the `/tusk` handoff, preserve the current checkout exactly as-is. The development work must happen in the task-owned workspace that `/tusk` Step 2 creates with `tusk task-worktree create <id> <brief-description-slug>`; do not run `tusk branch` directly from the current checkout, and do not allow dirty unrelated files in the current checkout to be auto-stashed as part of address-issue startup. If task-worktree creation is unavailable or fails, stop and surface the failure instead of falling back to branch-first work. Only proceed once you are operating from the returned `workspace_path` or an already-recorded workspace for this task.

Immediately invoke the `/tusk` workflow for the newly created task. Follow the "Begin Work on a Task" instructions from the tusk skill:

```
Read file: <base_directory>/../tusk/SKILL.md
```

Then execute those instructions starting at **"Begin Work on a Task (with task ID argument)"** using the `task_id` from Step 6. Do not wait for additional user confirmation — proceed directly into the development workflow.

**IMPORTANT: Execute /tusk steps 1–11 only. Do NOT execute step 12 (merge/retro).** Stop after step 11 (`/review-commits` or the lint step) — this skill owns merge, issue close, and retro as steps 8–10 below.

**Mid-task criteria management** (mark done, group with commits, skip inapplicable, skip-verify) follows /tusk's Step 7 verbatim. In particular: if a criterion does not apply to the implementation path you chose (e.g., the issue describes "do X OR document why exempt" and you did X), use `tusk criteria skip <cid> --reason "..."`, NOT `tusk criteria done <cid> --skip-verify` — the latter stamps the criterion with an unrelated commit hash and pollutes the audit trail. The commit-time scope guard from /tusk Step 7 also applies — issue-derived edits touching files outside the task's referenced paths require `TUSK_SCOPE_GUARD_BYPASS=1` or `tusk commit --skip-verify`.

Hold onto the `session_id` returned by `tusk task-start` in step 1 of the /tusk workflow — it is required in step 8 below.

## Step 7.5: Polarity Check on Stored Failing-Test Specs

Before merging, mark any remaining `test`-type acceptance criteria done — `tusk criteria done <cid>` re-runs the stored `verification_spec` against the current (now-fixed) code and only marks it done on exit 0. Step 4.1's pre-creation sandbox confirmed the spec was *runnable*; it did NOT confirm the polarity (exit 0 ≡ "fixed", nonzero ≡ "broken"). Assertion-style specs (`test -z "$(...)"`, `test ! -e ...`, leading `!`) exit nonzero on broken AND fixed code — Step 4.1 reads the broken-state nonzero as "fails as expected" and stores the spec verbatim, then `tusk criteria done` blocks merge indefinitely because the same spec still exits nonzero against the fix. This step catches that mismatch authoritatively (issue #642, original incident TASK-287 / criterion #1291).

Run unconditionally — the fetch is cheap and produces an empty result set when no test-type criteria remain.

### Procedure

1. Fetch every open `test`-type criterion still attached to the task:

   ```bash
   TEST_ROWS=$(tusk -json "SELECT id, criterion, verification_spec FROM acceptance_criteria WHERE task_id = <id> AND criterion_type = 'test' AND verification_spec IS NOT NULL AND is_completed = 0 AND is_deferred = 0")
   ```

   If `TEST_ROWS` is `[]`, skip the rest of this step.

2. For each row's `id` (`<cid>`), run:

   ```bash
   tusk criteria done <cid>
   ```

   `tusk criteria done` re-runs the spec from the repo root against the fixed code and only marks the criterion done on exit 0.

3. **Exit 0** — the spec passes against the fixed code; the criterion is now marked done. Move on.

4. **Exit 1 (verification failed)** — polarity mismatch suspected. The spec either uses inverted assertion polarity (e.g. `test -z`, `test ! -e`, leading `!`) or describes a different failure than the implementation actually addressed. Surface to the implementer:

   > ⚠ **Polarity mismatch on criterion #<cid>.** The stored spec exits nonzero against the fixed code:
   > ```
   > <verification_spec>
   > ```
   > Options:
   > - **invert** — re-run the spec wrapped as `bash -c '! ( <verification_spec> )'`. If it now exits 0, mark the criterion done with `tusk criteria done <cid> --skip-verify --note "polarity inverted: original spec used assertion polarity, wrapped form passes"`. The stored `verification_spec` is left as-is — the rationale lives in `skip_note`, which is durable and surfaces in retro/audit queries; live mutation would require a new tusk subcommand and is out of scope here.
   > - **skip** — defer the criterion via `tusk criteria skip <cid> --reason "polarity mismatch — assertion-style spec from issue body, behavior verified manually"`.
   > - **as-is** — accept that the spec is correct in shape but cannot auto-verify (e.g., the implementation reframed the failure differently); mark done with `tusk criteria done <cid> --skip-verify --note "polarity mismatch, behavior verified manually"`.

   In auto mode, default to **skip** — never silently invoke `--skip-verify` against a spec known to fail, since that buries the polarity signal in the audit trail rather than acknowledging it explicitly. The **invert** path is only valid when the wrapped-`!` form actually exits 0 against the fixed code; if it still fails, fall back to **skip** (the assertion does not describe the bug we fixed).

5. **Other exit codes** — `tusk criteria done` returns 2 if the criterion does not exist (already deferred between fetch and re-run, or hand-deleted). Skip it and continue with the next row.

## Steps 8–10: Finalize (Run as an Unbroken Sequence — No User Confirmation Between Steps)

### Step 8: Merge

Detect whether work landed on a feature branch or directly on the default branch:

```bash
CURRENT_BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
```

- `CURRENT_BRANCH == DEFAULT_BRANCH` → skip `tusk merge`; the commit is already shipped.
- Otherwise → run `tusk merge <task_id> --session <session_id>`.

Then capture the commit SHA for Step 9 via `git log --oneline -1` (first token). If the project uses PR-based merges, also note the PR URL from the merge output or `gh pr list --state merged --limit 1`.

### Step 9: Close the GitHub Issue

```bash
gh issue close <number> --repo <owner/repo> --comment "Resolved in <commit_sha> — <pr_url_or_branch>. Tracked as tusk task #<task_id>."
```

**Avoid backticks and unescaped `$` in the comment body — no automatic guard covers this surface** — `--comment` values (and the `--body` of any `gh issue comment` fallback) are shell arguments, so zsh and bash expand backticks and `$VAR` / `$(...)` even inside double quotes. Drop markdown code ticks around identifiers (write `_resolve_stable_tusk_bin` and `bin/tusk-merge.py` as plain text, not in backticks) and avoid literal dollar signs unless every metacharacter is escaped deliberately. **Note:** `tusk commit` enforces this at the boundary via `_validate_message_metacharacters` (issue #881), but `gh` is an external tool tusk does not wrap — the substitution hazard remains entirely manual to avoid here. The same caveat applies to `tusk review add-comment` (`/review-commits` Step 5.1).

Use the `commit_sha` from Step 8 (include the PR URL if available, else the branch name). On failure, apply **Shared gh Failure Handling** from Step 5 — the already-closed retry posts the resolution note as a standalone comment and continues to Step 10.

### Step 10: Retro

After `tusk merge` exits 0, close out the `/tusk` skill-run opened in Step 7 (its `run_id` came from `tusk task-start` inside the `/tusk` Step 1 invocation — you captured it as `skill_run.run_id` in the returned JSON) so its cost is captured before `/retro` starts its own run:

```bash
tusk skill-run finish <run_id>
```

Then emit the canonical end-of-run summary so the user sees the identity/cost/duration/diff/criteria rollup before the retro findings:

```bash
tusk task-summary <task_id> --format markdown
```

Show it verbatim — do not re-render or summarize. `/retro` Step LR-3 assumes this block has already been printed and intentionally does not re-emit it.

Invoke `/retro <task_id>` immediately — do not ask "shall I run retro?". Pass the task id explicitly so `/retro` attributes cost to the task you just finalized rather than picking up whichever sibling worktree closed last (issue #805). Read and follow:

```
Read file: <base_directory>/../retro/SKILL.md
```
