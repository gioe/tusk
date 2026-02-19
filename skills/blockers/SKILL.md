---
name: blockers
description: Manage external blockers for tasks (add, list, resolve, remove)
allowed-tools: Bash
---

# Blockers Skill

Manages external blockers in the project task database (via `tusk` CLI). External blockers represent dependencies outside the codebase — waiting on data, approvals, infrastructure, or third-party services — that prevent a task from being worked on.

## Commands

### Add a blocker

```bash
tusk blockers add <task_id> "description of what's blocking" [--type data|approval|infra|external]
```

The `--type` flag categorizes the blocker (default: `external`):
- **`data`** — waiting on data or datasets
- **`approval`** — waiting on a decision or sign-off
- **`infra`** — waiting on infrastructure or environment setup
- **`external`** — waiting on an external party or service

Example:
```bash
tusk blockers add 42 "Waiting on API credentials from vendor" --type external
```

### List blockers for a task

```bash
tusk blockers list <task_id>
```

### Resolve a blocker

Mark a blocker as resolved (the blocking condition has been met):

```bash
tusk blockers resolve <blocker_id>
```

### Remove a blocker

Delete a blocker entirely (it was added in error):

```bash
tusk blockers remove <blocker_id>
```

### Show tasks with unresolved blockers

```bash
tusk blockers blocked
```

### Show all blockers

```bash
tusk blockers all
```

## Arguments

Parse the user's request to determine:
1. The subcommand (add, list, resolve, remove, blocked, all)
2. The task ID or blocker ID involved (if applicable)
3. The description and type (for add)

Then run the appropriate command from the examples above.
