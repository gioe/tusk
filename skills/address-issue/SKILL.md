---
name: address-issue
description: Fetch a GitHub issue, create a tusk task from it, and work through it with /tusk
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

# Address Issue Skill

Fetches a GitHub issue, converts it into a tusk task, and immediately begins working on it using the full `/tusk` workflow.

## Step 1: Parse the Issue Reference

Invoked with an optional issue number or full URL (e.g. `/address-issue 314`, `/address-issue https://github.com/gioe/tusk/issues/314`, or no argument to default to the newest open issue).

Extract the issue number:
- Full URL → parse the number from the path.
- Number only → use it directly.
- No argument → fetch the newest open issue:
  ```bash
  gh issue list --repo gioe/tusk --state open --limit 1 --json number,title
  ```
  If empty, report `> No open issues found in gioe/tusk.` and stop. Otherwise use the returned `number` and display: `> No issue specified — defaulting to newest open issue: #<number> "<title>"`

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

## Step 4.1: Extract Failing Test Criterion

Scan the issue body for a `## Failing Test` section. If present:

1. Extract the test spec. Prefer the **first** fenced block after the heading (triple- or single-backtick, with optional language tag); trim surrounding whitespace.

   **Plain-text fallback — if no fenced block is found**, treat the plain text between the `## Failing Test` heading and the next heading (or end of body) as the spec. Drop `#`-prefixed lines (shell comments) and trim whitespace. If non-empty, use as `<test_spec>` (sandbox flow in item 2 applies identically). If empty, fall through to item 3.

2. **Validate the extracted spec** — the spec is arbitrary shell code from a GitHub issue body and must be treated as untrusted. Show it to the user for approval, then run it in a sandbox so it cannot reach the host tusk repo (which is one `tusk`/`git` walk-up away), read environment secrets, or invoke project-installed tools.

   **a. Pre-flight: skip approval + sandbox when the spec's effective first token is off the sandbox PATH.**

   Before showing the approval prompt, identify the spec's *effective* first token — the executable that will actually run. For most specs this is just the first whitespace-delimited token. But specs wrapped in `bash -c '<body>'` or `sh -c '<body>'` are a recurring pattern in tusk's own issue templates (any regression spec that chains `tusk init && tusk task-insert ...` ends up wrapped this way), and the outer `bash`/`sh` is always on `/usr/bin:/bin` — checking it would always pass the fast-path and force the sandbox to run a wrapper whose inner project-tool calls would just exit 127. When the wrapper pattern is detected, peel it off and check the wrapper body's first token instead:

   ```bash
   FIRST_TOKEN=$(printf '%s' "$TEST_SPEC" | awk '!/^[[:space:]]*#/ {print $1; exit}')
   SECOND_TOKEN=$(printf '%s' "$TEST_SPEC" | awk '!/^[[:space:]]*#/ {print $2; exit}')
   if [[ ("$FIRST_TOKEN" == "bash" || "$FIRST_TOKEN" == "sh") && "$SECOND_TOKEN" == "-c" ]]; then
     # Wrapper detected. The body is the third positional arg, normally surrounded
     # by single or double quotes; strip them, then take its first token.
     WRAPPER_BODY=$(printf '%s' "$TEST_SPEC" | awk '{$1=""; $2=""; sub(/^ +/, ""); print}')
     WRAPPER_BODY=${WRAPPER_BODY#[\'\"]}
     WRAPPER_BODY=${WRAPPER_BODY%[\'\"]}
     CHECK_TOKEN=$(printf '%s' "$WRAPPER_BODY" | awk '!/^[[:space:]]*#/ {print $1; exit}')
   else
     CHECK_TOKEN="$FIRST_TOKEN"
   fi
   # GOTCHA (Issue #589): `command -v` special-cases path-containing names
   # (anything with a `/`) by checking the file relative to cwd — bypassing PATH
   # entirely. For relative paths like `bin/tusk`, `command -v` resolves against
   # the orchestrator's cwd (the project root) and reports success, but the
   # sandbox tempdir won't have that file and would exit 127. Short-circuit any
   # `/`-containing token to skip the sandbox directly without consulting
   # `command -v`.
   if [[ "$CHECK_TOKEN" == */* ]] || ! PATH=/usr/bin:/bin command -v "$CHECK_TOKEN" >/dev/null 2>&1; then
     # Effective token is unreachable on the sandbox PATH; the sandbox would exit 127.
     # Skip the approval prompt and the sandbox run, but score as "unverifiable" — the
     # author still supplied a concrete reproducer; we just can't validate it here.
     test_spec=null
     test_present="unverifiable"
   fi
   ```

   The check is a pure path-resolution lookup. The `*/*` glob short-circuits any token containing `/` (relative path like `bin/tusk` or absolute path) before invoking `command -v` — without it, `command -v bin/tusk` from the project root would report success against the cwd-relative file and bypass PATH entirely, even though the sandbox tempdir cannot reach that path (Issue #589). For bare command names, `command -v` reports whether `<token>` exists on `PATH=/usr/bin:/bin` without invoking it, so the spec is never executed at this stage.

   On skip, set `test_spec = null`, score `test_present` as `"unverifiable"`, do not add a test criterion in Step 6, and surface this one-line note. Do **not** route to item 3 below — that path is reserved for the section-absent case (`test_present="no"`). The `"unverifiable"` value exists because the author still supplied a concrete reproducer; we just can't run it under the sandbox's safety constraints, so the score sits between `"yes"` and `"no"` in `config.default.json` (`issue_scoring.factors.test_present`).

   > Spec invokes a non-PATH tool or path-referenced executable (`<token>`); skipping sandbox (would exit 127). Scoring `test_present` as `unverifiable` (the spec exists but can't be validated here). Failing-test verification deferred to `tusk criteria done` after task creation.

   When the wrapper-detection branch fires, `<token>` is the *inner* token (e.g. `tusk`) — not `bash`/`sh` — so the note correctly points at the actual unreachable executable.

   If the effective token DOES resolve on `/usr/bin:/bin` (e.g. `grep`, `python3`, `find`, or a `bash -c '<on-PATH-cmd> ...'` wrapper whose body's first token is itself on PATH), fall through to sub-item **b** below — the existing approval + sandbox flow runs unchanged. The fast-path is an addition, not a replacement.

   **b. Display the spec and request approval:**

   > The issue body's `## Failing Test` section contains this spec. If approved, it runs in an isolated sandbox (`env -i`, `PATH=/usr/bin:/bin`, no `.git` parent) — project tools like `tusk`, `pytest`, and any project-installed binary are off PATH and will exit 127, which Step 4.1 treats as a command error and discards the spec. Step 4.1 only checks that the spec is a *runnable, shell-safe command*; the authoritative "does it actually fail on the current code" check happens later via `tusk criteria done`.
   > ```
   > <test_spec>
   > ```
   > **Options:** `run` (execute in sandbox), `skip` (do not execute — treat as `test_spec = null`).

   Wait for the user's response. Treat anything other than an explicit `run` as `skip`. On skip, set `test_spec = null`, score `test_present` as `"unverifiable"`, do not add a test criterion in Step 6, and do not run the command. The `"unverifiable"` value applies because the `## Failing Test` section was syntactically present in the issue body — the user simply chose not to sandbox-validate it; the score (defined in `config.default.json` `issue_scoring.factors.test_present`) sits between `"yes"` (sandbox-validated) and `"no"` (section absent). Do **not** route to item 3 below — that path is reserved for the section-absent case (`test_present="no"`).

   **c. On approval, execute the spec in an isolated sandbox:**

   ```bash
   TEST_SPEC='<test_spec>'   # the extracted spec, single-quoted; see Step 6 for embedded-quote handling
   SANDBOX_DIR=$(mktemp -d)
   (
     cd "$SANDBOX_DIR" &&
     env -i HOME="$SANDBOX_DIR" PATH="/usr/bin:/bin" \
       bash -c "$TEST_SPEC" 2>"$SANDBOX_DIR/stderr.txt"
   )
   SPEC_EXIT=$?
   SPEC_STDERR=$(cat "$SANDBOX_DIR/stderr.txt")
   rm -rf "$SANDBOX_DIR"
   ```

   **Why each layer matters — preserve all three when editing this step:**
   - `cd "$SANDBOX_DIR"` — `tusk` and `git` both walk up from `$PWD` to find a repo root (see `find_repo_root` in `bin/tusk`). A throwaway tempdir has no `.git`, so the walk-up terminates inside the sandbox rather than discovering the host repo. Without this, a spec that calls `tusk commit` or `git` from the tusk source repo's cwd would execute against the real repo (observed in TASK-93).
   - `env -i` — drops inherited environment (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `TUSK_DB`, shell customizations) so the spec cannot read secrets or redirect writes to a different database via `TUSK_DB`.
   - `PATH="/usr/bin:/bin"` — keeps project-installed tools (`tusk`, `pytest`, venv-installed linters, etc.) off the search path. Invocations of those tools inside the spec fail with a command error rather than executing against real state.

   The sandbox narrows what Step 4.1 can validate: most legitimate specs call project tools that are now off-PATH, so they will exit with a command error rather than reproducing the bug. This is intentional. Step 4.1's job is only to confirm the spec is a *runnable, shell-safe command*; the authoritative "does it fail on the current codebase" check is delegated to `tusk criteria done` later, which runs the spec in the real project after the task is underway.

   Interpret the result:

   - **Exit nonzero, no command error** — spec fails as expected. Store as `test_spec` and proceed. (Before storing, verify the spec calls into the project under test — runs a CLI, imports a project module, references a real file. Self-contained specs with inline logic may exit nonzero yet pass trivially once that inline logic is fixed; surface this in Step 7 so the implementer validates manually.)
   - **Exit 0** — spec passes before any fix (self-contained demo, already-resolved issue, or a self-skip guard fired in the sandbox — e.g. `git diff` against a missing `.git` parent). Ask the implementer: discard (`test_spec=null`, score `test_present="no"`) or keep with a `(warning: passed before fix)` note appended (score `test_present="unverifiable"` — the spec was attempted but didn't reach validation logic, equivalent in epistemic value to a user-typed skip; this preserves the invariant that `test_present="yes"` means the bug was observed to fail under our own execution; this rule is also recorded in the Step 4.7 `test_present` table to keep the two locations aligned)?
   - **Command error** (exit 126/127, or stderr contains "command not found" / "syntax error") — not a runnable shell command. Set `test_spec=null`, score `test_present="no"`, and inform: > The `## Failing Test` spec produced a command error (`<first line of SPEC_STDERR>`). Treating as no failing test.

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

2. **Summarize** findings as a short bullet list before proceeding.

3. **Refine Step 4 fields**: sharpen `description` (name files/functions), tighten criteria to match real code structure, adjust `complexity` if warranted. Do **not** change `summary`, `priority`, or `domain` unless the investigation reveals a fundamental misclassification.

## Step 4.6: Reproducibility Check (bug-type only)

**Run this step only when `task_type = bug`.** Skip for all other task types.

Before presenting the proposal, quickly scan the codebase to confirm the bug is still present. Use at most 3 tool calls (Grep, Read, or Bash read-only). **Prefer invoking the affected code path directly** (e.g. running the actual command with a known input) over grepping for static markers — a live invocation surfaces regex bugs, off-by-one errors, and silent failures that grep-and-read miss. If you find clear evidence the bug is already fixed (e.g., the code path described in the issue no longer exists or has been corrected), surface this before proceeding:

> **Reproducibility note:** The issue may already be fixed — [brief explanation]. Do you still want to create a task?

Wait for user confirmation before proceeding to Step 5. If the bug is confirmed still present, or if you cannot determine either way within 3 calls, proceed without comment.

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
| `test_present` | Result from Step 4.1. Section absent → `"no"`. Section present and the spec was sandbox-executed to a non-zero exit (without a command-error signature) → `"yes"`. Section present but the spec was *not* sandbox-executed (Step 4.1.a fast-path skip for an off-PATH effective first token, or Step 4.1.b user-typed skip) → `"unverifiable"` — the issue author supplied a concrete reproducer but it can't be validated under the sandbox's safety constraints. Section present and the spec was sandbox-executed to exit 0 with the implementer choosing `keep` (the spec's self-skip guard fired in the sandbox tempdir — typically `git diff` against a missing `.git` parent — and the implementer kept the spec rather than discarding) → `"unverifiable"` — the spec was attempted but didn't reach validation logic, equivalent in epistemic value to a user-typed skip; this preserves the invariant that `"yes"` means we observed the bug fail under our own execution. (Step 4.1.c's exit-0 `discard` branch sets `test_present="no"` and is unaffected by this rule.) Section present and the spec was sandbox-executed but produced a command error (exit 126/127 from inside the body, or stderr containing `command not found` / `syntax error`) → `"no"` — distinct from the skip path because the spec was actually run and demonstrably malformed, not merely unsandboxable. **Only evaluate for `bug` and `defect` task types.** For all other task types (`docs`, `feature`, `refactor`, etc.), treat as N/A: contribution = 0 regardless of value. | `"yes"` / `"no"` / `"unverifiable"` |
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

If confirmed (or no duplicate found), insert with:

```bash
tusk task-insert "<summary>" "<description>" \
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

When in doubt, always use the variable form — it is safe for any `test_spec` that does not contain a double quote or backslash (which pytest selectors never do).

This criterion will be validated by running the spec as a shell command when `tusk criteria done <cid>` is called — it blocks closure if the command exits nonzero.

**Exit code 0** — success. Note the `task_id` from the JSON output.

**Exit code 1** — heuristic duplicate found. Report the matched task and stop:

> Skipped — duplicate of existing task #<id> (similarity <score>). Run `/tusk <id>` to work on it instead.

**Exit code 2** — error. Report and stop.

## Step 7: Begin Work (Steps 1–11 Only)

Immediately invoke the `/tusk` workflow for the newly created task. Follow the "Begin Work on a Task" instructions from the tusk skill:

```
Read file: <base_directory>/../tusk/SKILL.md
```

Then execute those instructions starting at **"Begin Work on a Task (with task ID argument)"** using the `task_id` from Step 6. Do not wait for additional user confirmation — proceed directly into the development workflow.

**IMPORTANT: Execute /tusk steps 1–11 only. Do NOT execute step 12 (merge/retro).** Stop after step 11 (`/review-commits` or the lint step) — this skill owns merge, issue close, and retro as steps 8–10 below.

Hold onto the `session_id` returned by `tusk task-start` in step 1 of the /tusk workflow — it is required in step 8 below.

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

Invoke `/retro` immediately — do not ask "shall I run retro?". Read and follow:

```
Read file: <base_directory>/../retro/SKILL.md
```
