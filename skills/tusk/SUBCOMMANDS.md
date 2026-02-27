# Tusk — Subcommand Reference

These subcommands are available when `/tusk` is invoked with arguments.

## Mark Task as Done

When called with `done <id>`:

```bash
tusk task-done <id> --reason completed
```

> **Note:** This is for manual out-of-workflow use only (e.g. closing a task without a branch). When finishing a task through the normal dev workflow, always use `tusk merge <id> --session $SESSION_ID` (step 12) — it handles session close, branch merge, push, and task closure together. Never pass `--session` to `tusk task-done`.

## View Task Details

When called with `view <id>`:

```bash
tusk -header -column "SELECT id, summary, description, status, priority, domain, assignee, task_type, complexity, priority_score, expires_at, closed_reason, created_at, updated_at FROM tasks WHERE id = <id>"
```

## List Top N Ready Tasks

When called with `list <n>` or just a number:

```bash
tusk -header -column "
SELECT id, summary, priority, complexity, domain, assignee
FROM v_ready_tasks
ORDER BY priority_score DESC, id
LIMIT <n>;
"
```

## Filter by Domain

When called with `domain <value>`: Get next ready task for that domain only.

## Filter by Assignee

When called with `assignee <value>`: Get next ready task for that assignee only.

## Show Blocked Tasks

When called with `blocked`:

```bash
tusk deps blocked
```

## Show In Progress Tasks

When called with `wip` or `in-progress`:

```bash
tusk -header -column "SELECT id, summary, priority, domain, assignee FROM tasks WHERE status = 'In Progress'"
```

## Preview Next Task (without starting)

When called with `preview`: Show the next ready task but do NOT start working on it.

```bash
tusk -header -column "
SELECT id, summary, priority, complexity, domain, assignee, description
FROM v_ready_tasks
ORDER BY priority_score DESC, id
LIMIT 1;
"
```

## Argument Parsing Summary

| Argument | Action |
|----------|--------|
| (none) | Get next ready task and automatically start working on it |
| `<id>` | Begin full workflow on task #id |
| `list <n>` | Show top N ready tasks |
| `done <id>` | Mark task as Done |
| `view <id>` | Show full task details |
| `domain <value>` | Filter next task by domain |
| `assignee <value>` | Filter next task by assignee |
| `blocked` | Show all blocked tasks |
| `wip` | Show all In Progress tasks |
| `preview` | Show next ready task without starting it |
