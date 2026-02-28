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
- **Lint rule candidates** — concrete, grep-detectable anti-patterns observed (e.g., calling a deprecated command, using a wrong pattern). Only include if an actual mistake occurred that a grep rule could prevent

Build a per-agent findings list:

```
Agent for TASK-<id> (<summary>):
  Friction: [...]
  Workarounds: [...]
  Tangential: [...]
  Failed approaches: [...]
  Lint rule candidates: [...]
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

#### Lint Rule Candidates (N total)
- [TASK-<id>] <finding>

### Proposed Actions
| # | Action | Type | Priority | Source |
|---|--------|------|----------|--------|
| 1 | <summary> | task/lint-rule/skip | <priority> | Cross-agent pattern / Single agent |
```

**Proposed action types:**
- **task** — create a new tusk task for this finding
- **lint-rule** — create a task to add a grep-detectable lint rule via `tusk lint-rule add`
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
   tusk task-insert "<summary>" "<description>" \
     --priority "<priority>" --task-type "<task_type>" \
     --domain "<domain>" --complexity "<complexity>" \
     --criteria "<criterion>"
   ```
   Omit `--domain` or `--assignee` if NULL/empty.

### Lint Rules

For each approved lint-rule action, create a task whose description contains the exact `tusk lint-rule add` invocation. The implementing agent runs the command.

The bar is high — only create a lint rule task if you observed an **actual mistake** that a grep rule would have caught.

```bash
tusk task-insert "Add lint rule: <short description>" \
  "Run: tusk lint-rule add '<pattern>' '<file_glob>' '<message>'" \
  --priority "Low" --task-type "<task_type>" --complexity "XS" \
  --criteria "tusk lint-rule add has been run with the specified pattern, glob, and message"
```

For `<task_type>`: use the project's config `task_types` array (from `tusk config`). Pick the entry that best fits a maintenance/tooling task (e.g., `maintenance`, `chore`, `tech-debt`, `infra` — whatever is closest in your project's list). If no entry is a clear fit, omit `--task-type` entirely.

Fill in `<pattern>` (grep regex), `<file_glob>` (e.g., `*.md` or `bin/tusk-*.py`), and `<message>` (human-readable warning) with the specific values from the finding.

## RA-6: Retro Summary

```markdown
### Retro Aggregation Complete

**Chain**: <head_task_id> (<total agents> agents analyzed)
**Cross-agent patterns**: N identified
**Findings**: N friction / N workarounds / N tangential / N failed approaches / N lint rule candidates
**Actions taken**: N tasks created, N lint-rule tasks created, N skipped
```

If no findings were extracted from any agent (all transcripts were clean), report:

> Clean chain run — no retro findings across <N> agents.

and skip RA-4 through RA-5.
