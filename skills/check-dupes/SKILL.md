---
name: check-dupes
description: Check for duplicate tasks in the SQLite database before creating new tasks. Use before any INSERT into the task database.
allowed-tools: Bash
---

# Check Duplicates Skill

Fast, deterministic pre-filter for textual near-matches. Uses character-level (SequenceMatcher) and token-level (Jaccard) similarity scoring. Run this **before** inserting any task as a safety net against creating textually similar duplicates.

**Limitation:** This heuristic catches near-identical wording but misses semantic duplicates (e.g., "Implement password reset flow" vs. "Add forgot password endpoint"). Skills that insert tasks (`/create-task`, `/retro`) should also review the existing backlog for semantic overlap during their analysis phase — the heuristic is a supplement, not a replacement, for LLM-based semantic review.

## Usage

```
/check-dupes check "<summary>" [--domain <domain>] [--threshold <float>] [--json]
/check-dupes scan [--domain <domain>] [--status <status>] [--threshold <float>] [--json]
/check-dupes similar <id> [--domain <domain>] [--threshold <float>] [--json]
```

## Commands

### `check "<summary>"`

Pre-insert gate. Checks if a summary is a duplicate of any open task.

```bash
tusk dupes check "<summary>" --domain <domain>
```

**When to use:** Before every `INSERT INTO tasks` — in `/next-task`, `/create-task`, or any manual task creation.

### `scan`

Find all duplicate pairs among open tasks.

```bash
tusk dupes scan --status "To Do"
```

**When to use:** During `/groom-backlog` to surface duplicates for cleanup.

### `similar <id>`

Find tasks related to a given task ID (uses a lower threshold of 0.6).

```bash
tusk dupes similar <id>
```

**When to use:** Exploratory — when investigating whether a task overlaps with other work.

## Interpreting Results

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | No duplicates found — safe to create |
| 1 | Duplicates found — skip or review |
| 2 | Error (task not found, DB error) |

### Thresholds

| Score | Interpretation | Action |
|-------|---------------|--------|
| >= 0.82 | Duplicate | Skip the INSERT |
| 0.60 - 0.82 | Partial overlap | Present to user for decision |
| < 0.60 | New | Safe to create |

### JSON Output

Use `--json` for programmatic consumption:

```json
{
  "duplicates": [
    {"id": 42, "summary": "...", "domain": "Backend", "similarity": 0.95}
  ]
}
```

## Integration Points

This skill is referenced by:
- **`/next-task`** — Before creating deferred tasks
- **`/groom-backlog`** — Scanning for duplicate pairs
