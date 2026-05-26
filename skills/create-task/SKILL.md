---
name: create-task
description: Break down freeform text (feature specs, meeting notes, bug reports) into structured tusk tasks with deduplication
allowed-tools: Bash, Read
---

# Create Task Skill

Takes arbitrary text input — feature specs, meeting notes, brainstorm lists, bug reports, requirements docs — and decomposes it into structured, deduplicated tasks in the tusk database.

## Step 1: Capture Input

The user provides freeform text after `/create-task`. This could be:
- A feature description or requirements list
- Meeting notes or brainstorm output
- A bug report or incident summary
- A pasted document or spec
- A simple one-liner for a single task

If the user didn't provide any text after the command, ask:

> What would you like to turn into tasks? Paste any text — feature specs, meeting notes, bug reports, requirements, etc.

## Step 2: Fetch Config and Backlog

Fetch everything needed for analysis in a single call:

```bash
tusk setup
```

This returns a JSON object with two keys:
- **`config`** — full project config (domains, task_types, agents, priorities, complexity, etc.). Store for use when assigning metadata. If a field is an empty list (e.g., `"domains": []`), that field has no validation — use your best judgment or leave it NULL.
- **`backlog`** — all open tasks as an array of objects. Hold in context for Step 3. The heuristic dupe checker (`tusk dupes check`) catches textually similar tasks, but you can catch **semantic** duplicates that differ in wording — e.g., "Implement password reset flow" vs. existing "Add forgot password endpoint" — which the heuristic would miss.

## Step 2.5: Bundled-Scope Pre-Check

Before drafting tasks, scan the input for **bundled-scope markers** that signal "this looks like one task but it's really N tasks glued together" (issue #782, original incident TASK-2178: a single L task `Add comedy-specific flourishes: room-history memory tile on comedian detail + clip-preview play button` had a description literally reading `(1) Room-history memory tile... (2) Clip-preview play button... Both require backend data coordinated with frontend presentation.` — accepted as-is, then decomposed and abandoned at /tusk pickup with zero code shipped).

A bundle is suspected if **any** of these patterns appears in the summary or description:

| Marker | Where | Example |
|---|---|---|
| ` + ` between two named features | summary | `Add A + B` |
| `: ` followed by a connector list | summary | `flourishes: X, Y, and Z` |
| Numbered enumeration `(1)` / `(2)` / `1.` / `2.` introducing distinct deliverables | description | `(1) memory tile (2) play button` |
| Quantity-connector phrases: `both X and Y`, `X as well as Y`, `two <nouns>`, `three <nouns>`, `each of` | description | `Both require backend data...` |

**Inverse — do NOT fire** on incidental connectives where one side is naturally subordinate to the other:

| Allowed pattern | Why it's not a bundle |
|---|---|
| `add X and update Y's docs` | Y's docs is a natural completion of X, not a sibling deliverable |
| `fix bug X and add regression test` | The regression test is verification of the fix, not a sibling feature |
| `refactor module and rename file` | The rename is incidental to the refactor |

When any bundling marker fires, surface an informational advisory naming the matched pattern verbatim — this is a heads-up, not a gate. The commit-time scope guard is the real enforcement boundary; bundled tasks fail loudly at commit time when the agent tries to commit the second deliverable outside the originally-named files, so this prompt is a UX courtesy that lets the operator decide before the draft instead of after the rejected commit.

> Heads up — input appears to bundle multiple deliverables (matched: `<verbatim quote of the marker>`). The scope guard will likely reject mid-task commits that wander outside the originally-named files, so a bundled task will hit friction at commit time. Consider splitting now.
>
> Options: **Show me the proposal first** *(default)* (continue to Step 3 unchanged; revisit after seeing the draft) / **Split** (decompose into N sibling tasks now) / **Keep as one** (proceed with the bundle as a single task).

On **Split**, treat each deliverable as its own task during Step 3. On **Show me first** or **Keep as one**, continue to Step 3 unchanged; the operator can revisit after Step 4's review.

## Step 3: Analyze and Decompose

Break the input into discrete, actionable tasks. For each task, determine:

| Field | How to Determine |
|-------|-----------------|
| **summary** | Clear, imperative sentence describing the deliverable (e.g., "Add login endpoint with JWT authentication"). Aim for ~100 chars; hard cap **150 chars** (enforced in Step 3.7). |
| **description** | Expanded context from the input — motivation, constraints, links to source material. Hard cap **1200 chars** (enforced in Step 3.7) — move acceptance criteria and step-by-step details out into the criteria list. |
| **priority** | Infer from language cues: "critical"/"urgent"/"blocking" → `Highest`/`High`; "nice to have"/"eventually" → `Low`/`Lowest`; default to `Medium`. Must be one of the configured priorities. |
| **domain** | Match to a configured domain based on the task's subject area. Leave NULL if no domains are configured or none fit. |
| **task_type** | Categorize as one of the configured task types (bug, feature, refactor, test, docs, infrastructure). Default to `feature` for new work, `bug` for fixes. For `test` and `docs`: use as `task_type` only when writing tests or docs **is the primary deliverable** — otherwise use acceptance criteria. See **Task Type Decision Guide** below. |
| **assignee** | Match to a configured agent if the task clearly falls in their area. Leave NULL if unsure. |
| **complexity** | Estimate effort: `XS` = partial session, `S` = 1 session, `M` = 2-3 sessions, `L` = 3-5 sessions, `XL` = 5+. Default to `M` if unclear. Must be one of the configured complexity values. |

### Description shape

Each task carries three text fields with distinct intents — keep them sharp. Blurring them produces brittle tasks that rot the moment any code edit lands:

| Field | Intent | Contains |
|-------|--------|----------|
| **summary** | **WHAT** — the deliverable in one imperative sentence | "Add JWT login endpoint", "Fix race in session-close" |
| **description** | **WHY** — motivation, constraints, links to source material | The user complaint, the audit finding, the design decision; links to RFCs, retros, PRs |
| **criteria** | **HOW** — testable conditions that prove the WHAT was delivered | "POST /auth/login returns 401 on bad password", "tests/integration/test_login.py passes" |

**Forbidden in descriptions:** file-and-line references (e.g. `bin/tusk:1234`, `skills/foo/SKILL.md:88`) and step-by-step implementation plans. Line numbers rot the moment any edit lands above them, and step-by-step plans over-anchor `/tusk` to a stale approach when the implementer should re-derive from current code. The description should explain *why* the work matters, not encode *how* a single past reading of the codebase suggested doing it.

**Encouraged:** stable identifiers as anchors — function and class names, config keys, table and column names, environment variables, constant names, and file paths *without* line numbers. These survive refactors and let the implementer use grep/LSP to locate current call sites:

- **Good:** "The `cmd_init` function in `bin/tusk` stamps `PRAGMA user_version` on fresh installs"
- **Avoid:** "bin/tusk:1456 sets the user_version pragma"
- **Good:** "Migration 55 added `tasks.fixes_task_id`; views need to be recreated to pick it up"
- **Avoid:** "See line 88 of bin/tusk-migrate.py for the migration logic"

**Paths are scope hints (TASK-471 / TASK-475).** Any file path you name in the description or criteria is **interpreted as a scope hint** — the commit-time scope guard (and the `task-insert` auto-extractor that seeds `task_scope`) read those paths as "this task is authorized to touch them." Be deliberate: only name paths the task will actually edit. Cite an external design doc by title or by section anchor, not by repo-relative path, unless the task will also modify it. Padding the description with unrelated path citations widens the implicit scope and undermines the guard's ability to flag accidental sprawl mid-task.

### Task Type Decision Guide

The key question: **Is this type the primary deliverable, or is it proof that another deliverable is done?**

| Task Type | Use as `task_type` when the work *is* this | Use as acceptance criterion when this *verifies* other work |
|-----------|---------------------------------------------|-------------------------------------------------------------|
| **bug** | The deliverable is fixing a defect — "Fix login crash on empty password" | A regression must not recur — "Empty password no longer crashes" |
| **feature** | The deliverable is new functionality | N/A — features are always tasks, never criteria |
| **refactor** | The deliverable is restructuring code without changing behavior | N/A — refactoring is always a primary deliverable, never just verification |
| **test** | Writing tests **is the goal** — "Write test suite for auth module" | Tests verify a feature is done — "All auth endpoints have passing tests" |
| **docs** | Writing docs **is the goal** — "Write v2→v3 migration guide" | Docs confirm completion — "API endpoint is documented in README" |
| **infrastructure** | The deliverable is tooling, CI, or infra changes | N/A — infra work is always a task |

**Key rule:** If removing the work would leave the *feature itself* incomplete → use as `task_type`. If removing it just removes *verification* of an already-complete feature → use as an acceptance criterion.

### Decomposition Guidelines

- **One task per deliverable** — if a feature has multiple distinct pieces of work, split them
- **Keep tasks actionable** — each task should be completable in a single focused session
- **Preserve context** — include relevant details from the source text in the description
- **Don't over-split** — trivial sub-steps that are naturally part of a larger task don't need their own row
- **Group related fixes** — multiple closely related bugs can stay as one task if they share a root cause
- **Check for semantic overlap** — compare each proposed task against the existing backlog (from Step 2b). If an existing task covers the same intent with different wording, flag it as a duplicate rather than proposing a new task

## Step 3.5: Pre-Verify Bug Test Failures

**Run this step only when the input describes a bug that claims a specific test is failing or references a pre-existing test failure.** Skip for all other input types.

Trigger signals (any one is sufficient):
- The input uses phrases like "pre-existing failing test", "test is failing", "failing test", "test fails", or names a specific test file or test function alongside a bug description
- The `task_type` determined in Step 3 is `bug` **and** the description references a test by name or path

If triggered:

1. **Detect the test command:**
   ```bash
   tusk test-detect
   ```
   If `confidence` is `"none"`, skip the rest of this step — no test runner could be identified.

2. **Run the referenced test.** Extract the test name, file, or pattern from the input and run it against the detected command. For example, if the test command is `pytest` and the input mentions `test_foo_bar`, run:
   ```bash
   <test_command> <test_reference>   # e.g. pytest tests/unit/test_foo.py::test_foo_bar
   ```
   If no specific test name or file can be identified from the input, skip the rest of this step — treat as indeterminate.
   Limit to 60 seconds. If the command times out or errors for reasons unrelated to test failure (e.g. import error, missing dependency), skip the rest of this step — treat as indeterminate.

3. **Evaluate the result:**
   - **Test fails (non-zero exit):** Failure confirmed. Proceed to Step 4 without comment — the bug is real.
   - **Test passes (exit 0):** Surface this before presenting the proposal:

     > **Pre-verification note:** The referenced test is currently **passing** on this branch — the failure described may not be pre-existing. Do you still want to create a bug task for it?

     Wait for the user's response. If they say no or cancel, stop. If they confirm, proceed to Step 4 with the original task fields unchanged.

## Step 3.6: Detect Fix / Follow-up Linkage

Before presenting the task list, scan each proposed task's **summary and description** for phrasing that signals it is a follow-up, rework, or fix of an earlier task:

- `fixes TASK-N`
- `follow-up from TASK-N` / `follow up from TASK-N`
- `retro follow-up from TASK-N`

Case-insensitive. When a match is found:

1. **Parse `N`** out of the phrase.
2. **Verify `N` exists** — check the backlog array from Step 2 first; if not present, run `tusk task-get N` to confirm it exists as a Done task. If `N` doesn't resolve to a real task, drop the linkage silently and continue (the phrasing was likely informal).
3. **Reject self-reference** — if `N` equals the proposed task's own future id (can't be known yet; use summary cross-check: if the input explicitly ties `N` to the same task being created, skip).
4. **Record `fixes_task_id = N`** on that task's metadata for use by Step 5.

**Ambiguity prompt** — if a single task's text mentions **two or more different** `TASK-N` identifiers via the above phrasing, do not auto-pick. Surface the ambiguity before Step 4:

> **Follow-up linkage ambiguous for task #<i>**: the description mentions both `TASK-<a>` and `TASK-<b>` as sources. Which one should this task link to? (Answer with the id, or say "none" to skip.)

Apply the user's answer to that task's `fixes_task_id` and continue.

**Borderline phrasing** — mere mentions like "see TASK-N" or "related to TASK-N" (without `fixes`, `follow-up from`, or `retro follow-up from`) do **not** qualify. Leave `fixes_task_id` unset in those cases.

## Step 3.7: Validate Length Limits

Before presenting the task list, verify every proposed task complies with the hard length caps:

- **summary** — at most **150 characters**
- **description** — at most **1200 characters**

These caps prevent bloated text from being re-sent on every `tusk task-list` and `tusk task-get` call. An audit found tasks where the entire description had been pasted verbatim into the summary field, producing 600+ char summaries that polluted every subsequent listing.

For each proposed task, count `len(summary)` and `len(description)`. If **either** exceeds its cap, refuse to insert that task — surface the violation and prompt the user before continuing:

> **Length violation in task #<i>** ("<short title>"):
> - summary: <S> chars (max 150) — over by <S - 150>
> - description: <D> chars (max 1200) — over by <D - 1200>
>
> How would you like to fix this? You can:
> - **Trim** — propose a shorter version (suggest one if useful)
> - **Split** — break the task into multiple smaller tasks (helpful when the description is long because it covers multiple deliverables)
> - **Move** detail from description into acceptance criteria (the criteria list has no length cap and is the right home for HOW-style content)
> - **Cancel** this task

Apply the user's chosen fix, recount lengths against the same caps (150 / 1200), and only proceed to Step 4 once **every** task is within both limits. Do not skip this validation — a task that exceeds either cap must not reach `tusk task-insert`.

## Step 3.8: Extract Scope Hints

Before presenting the task list, ask `tusk scope-hint` to derive the **proposed scope** for each task — the set of paths the task is authorized to touch, plus any signals that the task is unbounded (refactor / cross-cutting). Surfacing scope at planning time is what makes the commit-time scope guard actionable: the operator sees what scope the task will have *before* it ships, instead of discovering the boundary the first time a mid-task commit gets rejected.

For each proposed task, run:

```bash
tusk scope-hint \
  --summary "<summary>" \
  --description "<description>" \
  --task-type "<task_type>" \
  --domain "<domain>" \
  --criterion "<criterion 1 text>" [--criterion "<criterion 2 text>" ...] \
  --typed-spec "<typed-criterion spec 1>" [--typed-spec "<typed-criterion spec 2>" ...]
```

The command returns JSON of the shape:

```json
{
  "scope": ["bin/foo.py", "tests/integration/test_foo.py"],
  "creates": ["bin/foo.py"],
  "unbounded": false,
  "rationale": {
    "scope": "extracted from summary/description/criteria/specs",
    "creates": "description names a path as a new file/script"
  }
}
```

Three signals to act on:

- **`scope`** — file paths extracted from the prose. These are paths the task is expected to touch. They will be passed to `tusk task-insert` as `--scope` only when you want them recorded as `operator_declared`; otherwise, the `task-insert` auto-extractor will record the same set as `auto_derived` rows at insert time without an explicit flag (see Step 5). Either way, the operator should review the list — if any path looks accidental (a citation, an external link, a path the task does NOT mean to modify), edit the description in Step 4's review loop to remove it.

- **`creates`** — paths the description explicitly marks as new files (e.g. `"Create a new file bin/foo.py"`). These deserve `--creates` rather than `--scope` so the scope source is recorded accurately (the file does not exist yet — `auto_derived` would imply it does). Surface to the operator: *"This task proposes creating bin/foo.py — confirm?"*

- **`unbounded`** — `true` when the task is a refactor or contains cross-cutting signal phrases (`"across all"`, `"every skill"`, `"sweep through"`, etc.). An unbounded task short-circuits the commit-time scope guard, so flag it for explicit confirmation before insertion: *"This looks unbounded (`rationale.unbounded`). Pass `--unbounded` to short-circuit the scope guard, or split into per-area tasks instead?"*

The hint is advisory — the operator can confirm, edit, or override every suggestion in Step 4. Treat it as a starting point, not a verdict. If the suggested scope is clearly wrong (e.g. extracted a URL fragment that looked like a path), drop it.

## Step 4: Present Task List for Review

### Single-task fast path

If analysis produced **exactly 1 task**, use the compact inline format instead of the full table:

```markdown
## Proposed Task

**Add login endpoint with JWT auth** (High · api · feature · M · backend)
> Implement POST /auth/login that validates credentials and returns a JWT token. Include refresh token support.
>
> **Proposed scope** (from `tusk scope-hint`):
> - touches: `apps/api/auth/login.py`, `tests/integration/test_login.py`
> - creates: `apps/api/auth/login.py`
> - unbounded: no
```

Then ask:

> Create this task? You can **confirm**, **edit** (e.g., "change priority to Medium" or "remove tests/integration/test_login.py from scope"), or **remove** it.

### Multi-task presentation

If analysis produced **2 or more tasks**, show the full numbered table:

```markdown
## Proposed Tasks

| # | Summary | Priority | Domain | Type | Complexity | Assignee |
|---|---------|----------|--------|------|------------|----------|
| 1 | Add login endpoint with JWT auth | High | api | feature | M | backend |
| 2 | Add signup page with form validation | Medium | frontend | feature | S | frontend |

### Details

**1. Add login endpoint with JWT auth**
> Implement POST /auth/login that validates credentials and returns a JWT token. Include refresh token support.
>
> **Proposed scope:** touches `apps/api/auth/login.py`, `tests/integration/test_login.py` · creates `apps/api/auth/login.py` · unbounded: no

**2. Add signup page with form validation**
> Create signup form with email, password, and confirm password fields. Validate on blur and on submit.
>
> **Proposed scope:** touches `apps/web/signup.tsx`, `tests/integration/test_signup.py` · creates `apps/web/signup.tsx` · unbounded: no
```

Then ask:

> Does this look right? You can:
> - **Confirm** to create all tasks
> - **Remove** specific numbers (e.g., "remove 3")
> - **Edit** a task (e.g., "change 2 priority to High", "remove tests/integration/test_signup.py from 2's scope", "mark 1 as unbounded")
> - **Add** a task you think is missing

When the operator amends scope (e.g. *"remove tests/integration/test_signup.py from 2's scope"*), update the in-memory `scope` / `creates` / `unbounded` set you got from `tusk scope-hint`; don't re-run the hint — the operator's edits override the heuristic.

### For both paths

Wait for explicit user approval before proceeding. Do NOT insert anything until the user confirms.

## Step 5: Deduplicate, Insert, and Generate Criteria

For each approved task, generate **2–5 acceptance criteria** — concrete, testable conditions that define "done."

### Test-first default

**Default to `criterion_type=test` with a proposed pytest node ID** (e.g. `tests/integration/test_foo.py::TestBar::test_baz`) for any criterion that names a behavior, output shape, edge case, or invariant. The test does not need to exist yet — pinning the node ID at planning time forces the author to enumerate input cases before any code is written, and the criterion's contract becomes executable rather than prose.

Prose criteria can be satisfied by partial implementations that match the wording but miss edge cases. Pinning a test name forecloses that gap: `/tusk` cannot mark the criterion done until the named pytest invocation exits 0, so the implementer either writes the test or amends the criterion deliberately. There is no quiet path from "looks plausible" to "marked done."

The only criteria that should remain `manual` are genuine judgment calls: exploratory spikes, prose/UX/visual review, PR-description quality, design tradeoffs, and one-off manual operations. The **Manual fallback** subsection below enumerates these in detail.

Other typed criteria — `code` (presence/absence grep) and `file` (path glob) — remain useful for non-behavioral checks. All typed criteria auto-verify on `tusk criteria done`, removing reasoning cost from /tusk's output tokens; fewer-but-sharper typed criteria beat long manual checklists.

### Type-inference rubric

After drafting each criterion, pick its verification type. Most criteria become `test` (per the test-first default above) — the table below covers the remaining cases. Default to `manual` only when none of the rows fit *and* the criterion belongs in the Manual fallback list.

| Signal in the criterion text | Type | `spec` is | How verification runs |
|---|---|---|---|
| Names a **behavior, output shape, edge case, or invariant** that could be expressed as a pytest assertion (default per the test-first rule above) — or already mentions a test command, file, or name | `test` | The exact shell command that runs the test, typically a pytest node ID like `python3 -m pytest tests/foo/test_bar.py::TestBaz::test_quux -q` | Runs `spec`; pass = exit 0; 300s timeout |
| Mentions a **file path that should exist** (e.g. "CHANGELOG.md has an entry", "migration file in bin/ exists") | `file` | A glob pattern matching the expected path | Pass if **any** file matches |
| Mentions a **code symbol, string, or pattern that must (or must not) appear** (e.g. "`PRAGMA user_version = 56` stamped in cmd_init", "no raw `sqlite3` call in skills/") | `code` | A shell command (typically `grep -q …` or `! grep -q …`) whose exit code answers the question. **`grep` is line-based** — for assertions that must match across lines (most commonly "Python function accepts param X" against signatures formatted under PEP8/black, which routinely span multiple lines), use a small `ast.parse` Python one-liner instead; see the AST worked example below. Reaching for `grep -Pzq` (PCRE multi-line) is portable on GNU grep but BSD grep on macOS often lacks `-P`, so it is not a safe default. | Runs `spec`; pass = exit 0; 120s timeout |
| Anything else — visual review, design judgment, prose correctness, behavior in a UI | `manual` | — | None; /tusk asserts it during work |

### Worked `--typed-criteria` examples

One per non-manual type. These are valid arguments to `tusk task-insert` — copy the shape:

```bash
# test type — auto-runs the named test on `tusk criteria done`
--typed-criteria '{"text":"Migration test passes","type":"test","spec":"python3 -m pytest tests/integration/test_migrate_56.py -q"}'

# file type — auto-checks the glob matches at least one path
--typed-criteria '{"text":"Migration test file present","type":"file","spec":"tests/integration/test_migrate_*.py"}'

# code type — auto-greps for presence (or absence) of a symbol or pattern
--typed-criteria '{"text":"cmd_init stamps user_version 56","type":"code","spec":"grep -q \"PRAGMA user_version = 56\" bin/tusk"}'
--typed-criteria '{"text":"Skills do not call raw sqlite3","type":"code","spec":"! grep -rE \"(^|[|;&])\\s*sqlite3\\b\" .claude/skills/"}'

# code type, multi-line Python signature — `ast.parse` walks the AST so the
# match works whether the signature spans one line or twenty. Pass the file
# path, fn name, and param name as argv so you never interpolate user data
# into the Python source. Pipe through `tusk typed-criteria-build` to avoid
# quote-escaping hazards (the spec contains both `"` and `'`):
read -r -d '' SPEC <<'TUSK_EOF'
python3 -c "import ast,sys; t=ast.parse(open(sys.argv[1]).read()); assert any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==sys.argv[2] and any(a.arg==sys.argv[3] for a in n.args.args+n.args.kwonlyargs+n.args.posonlyargs) for n in ast.walk(t))" apps/scraper/src/laughtrack/foundation/infrastructure/http/client.py fetch_json scraper_key
TUSK_EOF
JSON=$(printf '%s' "$SPEC" | tusk typed-criteria-build --type code --text "fetch_json accepts scraper_key")
--typed-criteria "$JSON"
```

Use the same helper for `node -e` specs; otherwise shell/JSON escaping can
strip JS string-literal quotes before the value reaches
`acceptance_criteria.verification_spec`:

```bash
read -r -d '' SPEC <<'TUSK_EOF'
node -e "const fs=require(\"fs\"); const pkg=JSON.parse(fs.readFileSync(\"apps/web/package.json\",\"utf8\")); const happy=(pkg.devDependencies||{})[\"happy-dom\"]; if (!happy) process.exit(1);"
TUSK_EOF
JSON=$(printf '%s' "$SPEC" | tusk typed-criteria-build --type code --text "apps/web package manifests include patched happy-dom")
--typed-criteria "$JSON"
```

For `test` and `code`, `spec` is a shell command — exit 0 = pass; use `! …` to invert. For `file`, `spec` is a glob (recursive `**` works).

### Manual fallback

Reach for plain `--criteria` only when the check requires genuine human judgment and cannot be encoded as a test, code grep, or file glob. The test-first default does **not** apply in these cases:

- **Exploratory spikes / investigations** — the deliverable is a written takeaway, recommendation, or decision document, not code. There is nothing to assert on. The criterion is "the writeup answers the question" and only a human can judge that.
- **Prose / UX / visual review** — e.g. "the error message reads naturally to a non-technical user", "the screenshot looks right on mobile at 320px", "the README explains the migration clearly to someone seeing it for the first time". Wording quality and visual fidelity have no automated proxy.
- **PR descriptions and other prose deliverables** — the bar is the quality of the writing itself; no test substitutes for a careful read.
- **Design judgment calls** — whether an architectural choice is the right tradeoff, whether an API shape feels idiomatic, whether a refactor's diff size is justified by the win.
- **One-off manual operations** — a one-time DB inspection, environment check, or vendor-portal click-through that won't recur in CI.

For everything else — behaviors, output shapes, edge cases, regression coverage, file presence, code patterns, schema invariants — pin a test name (or a `code` / `file` spec). If a check feels like it should be manual but the *consequence* of getting it wrong is a recurring bug, write the test instead.

Examples:

```bash
--criteria "DOMAIN.md updated with schema entry for <table_name>"
--criteria "Error message in confirm dialog reads naturally to a non-technical user"
--criteria "PR description summarizes the migration steps and rollback plan"
```

For **bug** tasks, include a criterion that the failure case is resolved (often expressible as a typed `test` criterion — the failing test now passes). For **feature** tasks, include the happy path and at least one edge case. For any task that creates a new database table (or is in a schema-related domain), always include the manual criterion: "DOMAIN.md updated with schema entry for `<table_name>`".

### Dangerous Criterion Guard

Before inserting, apply these rules to every generated criterion:

**Prohibited patterns** — Never generate a criterion whose text contains any of the following. These commands run against the live environment and can destroy data or corrupt history:
- `tusk init --force` — wipes the live task database
- `git reset --hard` — discards uncommitted work
- `git push --force` / `git push -f` — overwrites remote history
- `rm -rf` — recursive deletion
- `DROP TABLE` / `DROP DATABASE` — destructive SQL

**Init verification redirect** — If the task involves verifying `tusk init` behavior (e.g., "init creates the schema correctly", "init recreates the DB under --force"), do **not** generate a criterion that runs `tusk init` against the live database. Instead, use the integration test suite, which spins up a temporary DB automatically:

> `python3 -m pytest tests/integration/ -k test_init -q` passes

**Warning on detection** — If any generated criterion matches a prohibited pattern, display a warning and stop before inserting:

> ⚠️ **Dangerous criterion detected**: The proposed criterion `"<criterion text>"` contains a destructive command (`<pattern>`). This would run against the live database and could cause data loss. Replace it with a safer alternative (e.g., an integration test assertion) before inserting.

Revise the criterion and present it to the user for approval before proceeding to insertion.

Then insert the task with criteria in a single call using `tusk task-insert`. This validates enum values against config, runs a heuristic duplicate check internally, and inserts the task + criteria in one transaction. Pass the scope decisions confirmed in Step 4 as `--scope` / `--creates` / `--unbounded` flags — the operator's review is the gate, not the heuristic:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --assignee "<assignee>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --criteria "<criterion 2>" \
  --criteria "<criterion 3>" \
  --creates "<creates path>" \
  --scope "<additional scope path operator confirmed>"
```

**Scope-flag rules** (consumed from the confirmed Step 3.8 + Step 4 state):

- **`--unbounded`** — pass when the operator confirmed the task is cross-cutting. When set, omit `--scope` and `--creates` entirely; the unbounded sentinel short-circuits the commit-time scope guard regardless of the other rows.
- **`--creates "<path>"`** — repeat once per path the operator confirmed as newly-created. These should be paths that do not yet exist on disk.
- **`--scope "<path>"`** — repeat once per **additional** path the operator explicitly authorized that the description does not name. Paths the description already names will be auto-extracted by `task-insert` as `auto_derived` rows; do not re-list them under `--scope` (it would create duplicate scope rows with different source attribution, which clutters audit trails).
- **Removed scope** — if the operator dropped a path the description still mentions, edit the description before insertion so the auto-extractor does not re-introduce it. The auto-extractor is path-agnostic; it cannot tell that a path was deliberately excluded.

After insertion succeeds, confirm the derived scope was recorded as expected:

```bash
tusk scope list <task_id>
```

Show the resulting list to the operator so they can sanity-check the final state before moving on. If anything looks wrong (auto-extractor picked up an unintended path, `--unbounded` was omitted by mistake), the operator can amend immediately via `tusk scope add` / `tusk scope` rather than discovering the gap when the first commit gets rejected.

If the task was linked to a source task in Step 3.6, append `--fixes-task-id <N>` so the follow-up relationship is persisted to `tasks.fixes_task_id`:

```bash
tusk task-insert "<summary>" "<description>" \
  --priority "<priority>" \
  --domain "<domain>" \
  --task-type "<task_type>" \
  --complexity "<complexity>" \
  --criteria "<criterion 1>" \
  --fixes-task-id <N>
```

Mix `--criteria` (manual) and `--typed-criteria` (test/file/code) freely in the same call — one flag per criterion. `--typed-criteria` takes a JSON object `{"text": "...", "type": "test|file|code|manual", "spec": "..."}`; non-manual types require `spec`. Pick the type using the rubric in this step: `test` → spec is the test-runner command (exit 0 = pass); `file` → spec is a glob (passes if any file matches); `code` → spec is a `grep -q` (or `! grep -q`) command (exit 0 = pass).

Omit `--domain` or `--assignee` entirely if the value is NULL/empty — do not pass empty strings.

### Exit code 0 — Success

The command prints JSON with `task_id` and `criteria_ids`. Use the `task_id` for dependency proposals in Step 7.

### Exit code 1 — Duplicate found → Skip

The command prints JSON with `matched_task_id` and `similarity`. Report which existing task matched:

> Skipped "Add login endpoint with JWT auth" — duplicate of existing task #12 (similarity 0.87)

### Exit code 2 — Error

Report the error and skip.

## Step 7: Propose Dependencies

Skip this step if:
- Zero tasks were created (all were duplicates), OR
- Exactly **one** task was created (single-task fast path — no inter-task dependencies to propose, and checking against the backlog adds ceremony for the most common use case)

If **two or more** tasks were created, analyze for dependencies. Load the dependency proposal guide:

```
Read file: <base_directory>/DEPENDENCIES.md
```

Then follow its instructions.

## Step 8: Report Results

After processing all tasks, show a summary:

```markdown
## Results

**Created**: 3 tasks (#14, #15, #16)
**Skipped**: 1 duplicate (matched existing #12)
**Dependencies added**: 2 (#16 → #14 (blocks), #17 → #14 (contingent))

| ID | Summary | Priority | Domain |
|----|---------|----------|--------|
| 14 | Add signup page with form validation | Medium | frontend |
| 15 | Fix broken CSS on mobile nav | High | frontend |
| 16 | Add rate limiting middleware | Medium | api |
```

Include the **Dependencies added** line only when Step 7 was executed (i.e., two or more tasks were created). If Step 7 was skipped (all duplicates, single-task fast path, or user skipped all dependencies), omit the line. If dependencies were proposed but the user removed some, only list the ones actually inserted.

### Zero-criteria check

After displaying the summary, verify that every created task has at least one acceptance criterion. For each created task ID, run:

```bash
tusk criteria list <task_id>
```

If any task has **zero criteria**, display a warning:

> **Warning**: Tasks #14, #16 have no acceptance criteria. Go back to Step 6 and generate criteria for them before moving on.

Do not proceed past this step until all created tasks have at least one criterion.

Then, **conditionally** show the updated backlog:

- If **more than 3 tasks were created**, show the full backlog so the user can see where the new tasks landed:

  ```bash
  tusk -header -column "SELECT id, summary, priority, domain, task_type, assignee FROM tasks WHERE status = 'To Do' ORDER BY priority_score DESC, id"
  ```

- If **3 or fewer tasks were created**, show only a count to save tokens:

  ```bash
  tusk "SELECT COUNT(*) || ' open tasks in backlog' FROM tasks WHERE status = 'To Do'"
  ```
