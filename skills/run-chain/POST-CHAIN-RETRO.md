# Post-Chain Retro Aggregation

Runs after the chain completes. Reads all agent output files, extracts learnings, identifies cross-agent patterns, and produces a consolidated retro report.

## Prerequisites

You should have a list of **all agent output file paths** collected during Steps 3 and 4 (one per agent spawned, including the head task agent and every wave agent). Skipped or aborted tasks without output files are excluded.

## RA-1: Collect Agent Transcripts

For each output file path, read the file:

```
Read file: <output_file_path>
```

If a file is missing or empty, note it and continue with the remaining files.

## RA-2: Extract Findings Per Agent

For each agent transcript, extract findings into these categories:

- **Friction points** — confusing instructions, missing context, repeated mistakes, skill gaps encountered
- **Workarounds** — manual steps the agent had to take that could be automated or codified
- **Tangential issues** — bugs, tech debt, test failures, or architectural concerns discovered out of scope
- **Failed approaches** — strategies the agent tried that didn't work, and why
- **Conventions** — generalizable heuristics the agent discovered or relied on (file coupling patterns, naming conventions, workflow patterns)

Build a per-agent findings list:

```
Agent for TASK-<id> (<summary>):
  Friction: [...]
  Workarounds: [...]
  Tangential: [...]
  Failed approaches: [...]
  Conventions: [...]
```

## RA-3: Identify Cross-Agent Patterns

Compare findings across all agents. A **cross-agent pattern** is any finding that appears in two or more agent transcripts (even if worded differently). These are higher-confidence signals because multiple independent agents encountered the same issue.

Examples:
- Same lint false positive hit by multiple agents
- Same confusing instruction in a skill tripped up multiple agents
- Same file or module caused friction for multiple agents
- Same workaround was independently discovered by multiple agents

Mark cross-agent patterns with the count of agents that encountered them.

## RA-4: Present Consolidated Report

Display the report to the user:

```markdown
## Post-Chain Retro: Chain <head_task_id>

### Cross-Agent Patterns (appeared in 2+ agents)
| # | Pattern | Agents | Category |
|---|---------|--------|----------|
| 1 | <description> | TASK-<id>, TASK-<id> | Friction/Workaround/etc. |

### All Findings by Category

#### Friction Points (N total)
- [TASK-<id>] <finding>
- [TASK-<id>, TASK-<id>] <cross-agent finding> **(pattern)**

#### Workarounds (N total)
- [TASK-<id>] <finding>

#### Tangential Issues (N total)
- [TASK-<id>] <finding>

#### Failed Approaches (N total)
- [TASK-<id>] <finding>

#### Conventions Discovered (N total)
- [TASK-<id>] <finding>

### Proposed Actions
| # | Action | Type | Priority | Source |
|---|--------|------|----------|--------|
| 1 | <summary> | task/convention/skip | <priority> | Cross-agent pattern / Single agent |
```

**Proposed action types:**
- **task** — create a new tusk task for this finding
- **convention** — write to `tusk/conventions.md`
- **skip** — informational only, no action needed

Cross-agent patterns should default to higher priority than single-agent findings.

Ask the user to **approve**, **edit**, or **remove** proposed actions before applying.

## RA-5: Apply Approved Actions

### Tasks

For each approved task action:

1. Run dupe check:
   ```bash
   tusk dupes check "<proposed summary>"
   ```

2. If no duplicate, insert:
   ```bash
   tusk "INSERT INTO tasks (summary, description, status, priority, domain, task_type, complexity, created_at, updated_at)
     VALUES ($(tusk sql-quote '<summary>'), $(tusk sql-quote '<description>'), 'To Do', '<priority>', '<domain_or_NULL>', '<task_type>', '<complexity_or_NULL>', datetime('now'), datetime('now'))"
   ```

### Conventions

For each approved convention action, check existing conventions:

```bash
tusk conventions
```

Skip any convention already captured. For new conventions, append to `tusk/conventions.md`:

```markdown

## <short title>
_Source: run-chain <head_task_id> — <YYYY-MM-DD>_

<one-to-two sentence description of the convention and when it applies>
```

## RA-6: Retro Summary

```markdown
### Retro Aggregation Complete

**Chain**: <head_task_id> (<total agents> agents analyzed)
**Cross-agent patterns**: N identified
**Findings**: N friction / N workarounds / N tangential / N failed approaches / N conventions
**Actions taken**: N tasks created, N conventions written, N skipped
```

If no findings were extracted from any agent (all transcripts were clean), report:

> Clean chain run — no retro findings across <N> agents.

and skip RA-4 through RA-5.
