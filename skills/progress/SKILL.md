---
name: progress
description: Log a progress checkpoint from the latest git commit for a task
allowed-tools: Bash
---

# Progress Skill

Logs a progress checkpoint for a task by capturing the latest git commit (hash, message, changed files) into the `task_progress` table. This enables context recovery when resuming a task in a new session.

## Usage

```bash
tusk progress <task_id> [--next-steps "what remains to be done"]
```

The `--next-steps` flag is optional but strongly encouraged — it records what still needs to happen, making it much easier for a future session to pick up where you left off. If the user omitted it, prompt them to provide it before completing.

Example:
```bash
tusk progress 42 --next-steps "Wire up the DELETE endpoint and add tests"
```

