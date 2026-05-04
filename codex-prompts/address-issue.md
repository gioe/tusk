# Address Issue — Fetch a GitHub Issue, Create a Task, Work It (Codex)

Fetches a GitHub issue, converts it into a tusk task, and immediately
begins working on it using the full task workflow.

> **Conventions:** Run `tusk conventions search <topic>` for project
> rules. Do not restate convention text inline — it drifts from the DB.

> Use `create-task.md` for free-form decomposition. This prompt is the
> issue-driven path: it inserts a single task derived from one issue,
> then runs `tusk.md` end-to-end against it.

## Step 1: Parse the Issue Reference

Invoked with an optional issue number or full URL (e.g.
`/address-issue 314`,
`/address-issue https://github.com/gioe/tusk/issues/314`, or no argument
to default to the newest open issue).

Extract the issue number:
- Full URL → parse the number from the path.
- Number only → use it directly.
- No argument → fetch the newest open issue:
  ```bash
  gh issue list --repo gioe/tusk --state open --limit 1 --json number,title
  ```
  If empty, report `> No open issues found in gioe/tusk.` and stop.
  Otherwise use the returned `number` and display:
  `> No issue specified — defaulting to newest open issue: #<number> "<title>"`.

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

Store the `config` (domains, task_types, agents, priorities, complexity)
and `backlog` (for duplicate detection).

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

Generate **3–7 acceptance criteria** from the issue body — concrete,
testable conditions. For bug issues, always include a criterion that
the failure case is resolved and a regression test criterion.

## Step 4.1: Extract Failing Test Criterion

Scan the issue body for a `## Failing Test` section. If present:

1. Extract the test spec. Prefer the **first** fenced block after the
   heading (triple- or single-backtick, with optional language tag);
   trim surrounding whitespace.

   **Plain-text fallback** — if no fenced block is found, treat the
   plain text between the `## Failing Test` heading and the next
   heading (or end of body) as the spec. Drop `#`-prefixed lines (shell
   comments) and trim whitespace. If non-empty, use as `<test_spec>`.
   If empty, fall through to item 3.

2. **Validate the extracted spec** — the spec is arbitrary shell code
   from a GitHub issue body and must be treated as untrusted. Show it
   to the user for approval, then run it in a sandbox so it cannot
   reach the host tusk repo (which is one `tusk`/`git` walk-up away),
   read environment secrets, or invoke project-installed tools.

   **a. Display the spec and request approval:**

   > The issue body's `## Failing Test` section contains this spec.
   > If approved, it runs in an isolated sandbox (`env -i`,
   > `PATH=/usr/bin:/bin`, no `.git` parent) — project tools like
   > `tusk`, `pytest`, and any project-installed binary are off PATH
   > and will exit 127, which this step treats as a command error and
   > discards the spec. Step 4.1 only checks that the spec is a
   > *runnable, shell-safe command*; the authoritative "does it
   > actually fail on the current code" check happens later via
   > `tusk criteria done`.
   > ```
   > <test_spec>
   > ```
   > **Options:** `run` (execute in sandbox), `skip` (do not execute —
   > treat as `test_spec = null`).

   Wait for the user's response. Treat anything other than an explicit
   `run` as `skip`. On skip, set `test_spec = null`, score
   `test_present` as `"no"`, and proceed as if no `## Failing Test`
   section were found (item 3 below) — do not run the command.

   **b. On approval, execute the spec in an isolated sandbox:**

   ```bash
   TEST_SPEC='<test_spec>'   # the extracted spec, single-quoted
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

   **Why each layer matters — preserve all three when editing this
   step:**
   - `cd "$SANDBOX_DIR"` — `tusk` and `git` both walk up from `$PWD`
     to find a repo root. A throwaway tempdir has no `.git`, so the
     walk-up terminates inside the sandbox rather than discovering
     the host repo.
   - `env -i` — drops inherited environment (`GITHUB_TOKEN`,
     `ANTHROPIC_API_KEY`, `TUSK_DB`, shell customizations) so the
     spec cannot read secrets or redirect writes to a different
     database.
   - `PATH="/usr/bin:/bin"` — keeps project-installed tools off the
     search path. Invocations of those tools inside the spec fail
     with a command error rather than executing against real state.

   Interpret the result:

   - **Exit nonzero, no command error** — spec fails as expected.
     Store as `test_spec` and proceed. (Before storing, verify the
     spec calls into the project under test — runs a CLI, imports a
     project module, references a real file. Self-contained specs
     with inline logic may exit nonzero yet pass trivially once that
     inline logic is fixed; surface this in Step 7 so the implementer
     validates manually.)
   - **Exit 0** — spec passes before any fix. Ask the implementer:
     discard (`test_spec=null`, `test_present="no"`) or keep with a
     `(warning: passed before fix)` note appended?
   - **Command error** (exit 126/127, or stderr contains "command
     not found" / "syntax error") — not a runnable shell command.
     Set `test_spec=null`, score `test_present="no"`, and inform:
     > The `## Failing Test` spec produced a command error
     > (`<first line of SPEC_STDERR>`). Treating as no failing test.
   - **Interpreter wrapper bypass** (exit nonzero AND NOT 126/127,
     with stderr containing one of the canonical missing-executable
     signatures from a language interpreter) — the spec is wrapped
     in a runtime (`python3 -c '<body>'`, `node -e '<body>'`,
     `ruby -e '<body>'`, `perl -e '<body>'`, etc.) whose interpreter
     itself runs cleanly on `/usr/bin:/bin` but whose body
     subprocesses an unreachable project tool. The "Command error"
     branch above only fires for exit 126/127; the language runtime
     instead exits 1 and surfaces the missing executable through its
     own exception machinery. Recognize this case by these stderr
     signatures, extracting `<token>`:
     - **Python** — `FileNotFoundError: [Errno 2] No such file or
       directory: '<token>'`
     - **Node** — `Error: spawn <token> ENOENT` or trailing
       `<token> ENOENT`
     - **Ruby** — `Errno::ENOENT: No such file or directory - <token>`
     - **Perl** — `Can't exec "<token>": No such file or directory`
     - **Generic child-process** — any line ending
       `<token>: No such file or directory` where `<token>` is a bare
       command name (no path component)

     Strip any path component from `<token>` (e.g. `bin/tusk` →
     `tusk`) and check whether the basename resolves on
     `PATH=/usr/bin:/bin` via `command -v`. If it does NOT resolve,
     the inner subprocess could not validate under the sandbox's
     safety constraints — the sandbox cannot tell whether the bug
     actually fails. Set `test_spec=null`, score `test_present="no"`,
     and inform:
     > The `## Failing Test` spec is an interpreter wrapper whose
     > inner subprocess could not reach `<token>`
     > (sandbox PATH = `/usr/bin:/bin`). The bug was not reproduced
     > under sandbox; treating as no failing test. Failing-test
     > verification deferred to `tusk criteria done` after task
     > creation.

     If the extracted `<token>` IS on `/usr/bin:/bin` (the inner
     subprocess called a system tool that genuinely failed) or no
     recognized signature matches the stderr, fall through to the
     "Exit nonzero, no command error" bullet above (treat as a real
     failure). Adding a new interpreter or runtime is a one-line
     change — append the language's canonical missing-executable
     signature to the table above.

3. **If no `## Failing Test` section is found**, set
   `test_spec = null`. No test criterion is added in Step 6. For
   `bug`/`defect` task types, this lowers the Step 4.7 score via
   `test_present`; for other task types, `test_present` is N/A.

## Step 4.5: Optional Codebase Investigation

**Skip if complexity is XS or S.** Only run for M, L, or XL.

Ask the user:

> Before presenting the proposal, should I investigate the codebase for
> context? (**yes** / **no**, default: no)

Treat any non-`yes` response as skip. On **yes**:

1. **Read-only investigation.** Tools: `Read`, `Grep`, `Glob`, and
   read-only `Bash` (tusk CLI queries, `ls`, directory inspection — no
   writes, no edits, no commits). Cap at ~10 tool calls; summarize
   even if incomplete. Look for:
   - Files/functions tied to the issue's subject (search by keyword,
     class, config key).
   - Existing tests for the affected paths.
   - Established conventions for similar features.
   - Any partial implementation already present.
   - Related tusk tasks:
     `tusk task-list --format json | jq '.[] | select(.summary | ascii_downcase | contains("<keyword>"))'`

2. **Summarize** findings as a short bullet list before proceeding.

3. **Refine Step 4 fields:** sharpen `description` (name files /
   functions), tighten criteria to match real code structure, adjust
   `complexity` if warranted. Do **not** change `summary`, `priority`,
   or `domain` unless the investigation reveals a fundamental
   misclassification.

## Step 4.6: Reproducibility Check (bug-type only)

**Run this step only when `task_type = bug`.** Skip for all other task
types.

Before presenting the proposal, quickly scan the codebase to confirm
the bug is still present. Use at most 3 tool calls (Grep, Read, or
Bash read-only). **Prefer invoking the affected code path directly**
(e.g. running the actual command with a known input) over grepping
for static markers — a live invocation surfaces regex bugs, off-by-one
errors, and silent failures that grep-and-read miss. If you find clear
evidence the bug is already fixed, surface this before proceeding:

> **Reproducibility note:** The issue may already be fixed —
> [brief explanation]. Do you still want to create a task?

Wait for user confirmation before proceeding to Step 5. If the bug is
confirmed still present, or you cannot determine either way within 3
calls, proceed without comment.

## Step 4.7: Model Recommendation (Config-Driven Scoring)

Read `issue_scoring` from the config fetched in Step 3:

```
scoring    = config["issue_scoring"]
factors    = scoring["factors"]
thresholds = scoring["thresholds"]
```

Evaluate each factor and look up its score contribution from
`factors`:

| Factor key | Condition to evaluate | Value key |
|---|---|---|
| `test_present` | Was a `## Failing Test` section found in Step 4.1? **Only evaluate for `bug` and `defect` task types.** | `"yes"` / `"no"` |
| `pillar_aligned` | Does the issue align with the project pillars (run `tusk pillars list`)? If empty, skip (contribution = 0). | `"yes"` / `"no"` |
| `duplicate` | Is an open task already covering this issue (from Step 3 backlog)? Include the task ID in the rationale if yes. | `"yes"` / `"no"` |
| `in_scope` | Does the issue fit the project's stated purpose? | `"yes"` / `"no"` |
| `severity_high` | Does inaction risk data loss, user-facing breakage, or a security vulnerability? | `"yes"` / `"no"` |
| `issue_quality` | Is the report clear, reproducible, and actionable? | `"good"` / `"poor"` |

For each factor:
`contribution = factors[factor_key][value_key]`

Compute: `total = sum of all factor contributions`.

Assign verdict from thresholds:
- `total >= thresholds["address"]` → **Address**
- `total <= thresholds["decline"]` → **Decline**
- Otherwise → **Address** (borderline — still create and work the
  task; the score breakdown surfaces the uncertainty for the user).

Record the verdict, per-factor contributions, total, and a 1–2
sentence rationale for display in Step 5.

## Step 5: Present Proposed Task for Review

Open with a **Model Recommendation** block (including the score
breakdown from Step 4.7), then show the proposed task:

```markdown
### Model Recommendation

> **Recommendation: <Address / Decline>** — <1–2 sentence rationale from Step 4.7>
>
> **Score:** test_present: <±N>, pillar_aligned: <±N>, duplicate: <±N>, in_scope: <±N>, severity_high: <±N>, issue_quality: <±N> → **total: <N>** (Address ≥ <thresholds.address>, Decline ≤ <thresholds.decline>)

## Proposed Task from Issue #<N>

**<summary>** (<priority> · <domain> · <task_type> · <complexity>)
> <description preview — first 2 sentences>

**Acceptance Criteria:**
1. <criterion 1>
2. <criterion 2>
...
```

Then ask the user to choose, **bolding the option that matches the
Model Recommendation**. For a Decline recommendation, replace
"confirm" with "proceed anyway" in the prompt:

> Create this task? You can confirm (implement now), edit (e.g.,
> "change priority to High"), decline (close the issue without
> creating a task), or cancel.

The user retains full veto power — any option may be chosen
regardless of the recommendation. Wait for explicit approval before
inserting.

### Shared gh Failure Handling

Referenced by the Decline Path and Step 9. When a `gh issue close`
or `gh issue comment` call fails:

1. If the error contains `already in a 'closed'` state, retry the
   action as
   `gh issue comment <number> --repo <owner/repo> --body "<same body>"`.
2. If the retry also fails, or the original error was something else
   (permissions, locked issue, etc.), surface the manual URL and the
   message to paste:
   > Could not update issue #<N> automatically. Please visit
   > https://github.com/<owner/repo>/issues/<N> and add this comment:
   > "<body>"

Never abort the prompt on a gh failure — continue the flow with the
manual-URL fallback.

### Decline Path

If the user types **decline** (optionally followed by an inline
rationale, e.g. `decline out of scope`):

1. If no rationale was given, prompt the user to pick one: `out of
   scope`, `won't fix`, `already handled by TASK-<id>`,
   `duplicate of #<issue>`, or a free-text reason.

2. Close the issue:
   ```bash
   gh issue close <number> --repo <owner/repo> --comment "Declined: <rationale>"
   ```
   - Success → > **Declined** — Issue #<N> closed. Reason:
     <rationale>. No task created.
   - Failure → apply **Shared gh Failure Handling**.

3. **Do NOT insert a task.** Stop — do not proceed to Step 6.

## Step 6: Deduplicate and Insert

Check for semantic duplicates against the backlog from Step 3. If a
likely duplicate exists, surface it:

> Possible duplicate: existing task #<id> — "<summary>". Proceed
> anyway?

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

**If `test_spec` is set (from Step 4.1)**, append one additional
`--typed-criteria` argument to the insert command:

```bash
  --typed-criteria '{"text":"Failing test passes","type":"test","spec":"<test_spec>"}'
```

Replace `<test_spec>` with the extracted command verbatim.

**Single-quote escaping:** If `test_spec` contains a single quote
(e.g., a pytest selector like
`tests/test_foo.py::test_it's_broken`), the single-quoted JSON
wrapper above will break. In that case, assign the spec to a shell
variable and use double-quoted outer JSON with escaped inner quotes:

```bash
TEST_SPEC='tests/test_foo.py::test_it'"'"'s_broken'
  --typed-criteria "{\"text\":\"Failing test passes\",\"type\":\"test\",\"spec\":\"$TEST_SPEC\"}"
```

When in doubt, always use the variable form — it is safe for any
`test_spec` that does not contain a double quote or backslash (which
pytest selectors never do).

This criterion will be validated by running the spec as a shell
command when `tusk criteria done <cid>` is called — it blocks closure
if the command exits nonzero.

**Exit code 0** — success. Note the `task_id` from the JSON output.

**Exit code 1** — heuristic duplicate found. Report the matched task
and stop:

> Skipped — duplicate of existing task #<id> (similarity <score>).
> Run `tusk.md` for `<id>` to work on it instead.

**Exit code 2** — error. Report and stop.

## Step 7: Begin Work (Steps 1–11 only)

Immediately follow `tusk.md` for the newly created task. Execute the
"Begin Work on a Task" instructions starting at Step 1, using the
`task_id` from Step 6. Do not wait for additional user confirmation
— proceed directly into the development workflow.

**IMPORTANT: Execute `tusk.md` Steps 1–11 only. Do NOT execute Step
12 (merge/retro).** Stop after Step 11 (`review-commits.md` or the
lint step) — this prompt owns merge, issue close, and retro as Steps
8–10 below.

Hold onto the `session_id` returned by `tusk task-start` in Step 1 of
the tusk workflow — it is required in Step 8 below.

## Steps 8–10: Finalize (run as an unbroken sequence)

### Step 8: Merge

Detect whether work landed on a feature branch or directly on the
default branch:

```bash
CURRENT_BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(tusk git-default-branch)
```

- `CURRENT_BRANCH == DEFAULT_BRANCH` → skip `tusk merge`; the commit
  is already shipped.
- Otherwise → run `tusk merge <task_id> --session <session_id>`.

Then capture the commit SHA for Step 9 via `git log --oneline -1`
(first token). If the project uses PR-based merges, also note the PR
URL from the merge output or `gh pr list --state merged --limit 1`.

### Step 9: Close the GitHub Issue

```bash
gh issue close <number> --repo <owner/repo> --comment "Resolved in <commit_sha> — <pr_url_or_branch>. Tracked as tusk task #<task_id>."
```

Use the `commit_sha` from Step 8 (include the PR URL if available,
else the branch name). On failure, apply **Shared gh Failure
Handling** from Step 5 — the already-closed retry posts the
resolution note as a standalone comment and continues to Step 10.

### Step 10: Retro

After `tusk merge` exits 0, close out the tusk skill-run opened in
Step 7 (its `run_id` came from `tusk task-start` inside the tusk Step
1 invocation — you captured it as `skill_run.run_id` in the returned
JSON):

```bash
tusk skill-run finish <run_id>
```

Then emit the canonical end-of-run summary:

```bash
tusk task-summary <task_id> --format markdown
```

Show it verbatim — do not re-render or summarize.

Follow `retro.md` immediately — do not ask "shall I run retro?".
The retro prompt assumes this block has already been printed and
intentionally does not re-emit it.
