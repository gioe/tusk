# Next Task — Subcommand Reference

These subcommands are available when `/next-task` is invoked with arguments.

## Mark Task as Done

When called with `done <id>`:

```bash
tusk task-done <id> --reason completed
```

This closes open sessions, sets status to Done, and returns JSON including an `unblocked_tasks` array showing any tasks that are now ready to work on.

## View Task Details

When called with `view <id>`:

```bash
tusk -header -column "SELECT * FROM tasks WHERE id = <id>"
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
tusk -header -column "
SELECT t.id, t.summary, t.priority,
  (SELECT GROUP_CONCAT(d.depends_on_id) FROM task_dependencies d WHERE d.task_id = t.id) as blocked_by_tasks,
  (SELECT GROUP_CONCAT(eb.description, '; ') FROM external_blockers eb WHERE eb.task_id = t.id AND eb.is_resolved = 0) as blocked_by_external
FROM tasks t
WHERE t.status = 'To Do'
  AND (
    EXISTS (
      SELECT 1 FROM task_dependencies d
      JOIN tasks blocker ON d.depends_on_id = blocker.id
      WHERE d.task_id = t.id AND d.relationship_type = 'blocks' AND blocker.status <> 'Done'
    )
    OR EXISTS (
      SELECT 1 FROM external_blockers eb
      WHERE eb.task_id = t.id AND eb.is_resolved = 0
    )
  )
ORDER BY t.id
"
```

## Show In Progress Tasks

When called with `wip` or `in-progress`:

```bash
tusk -header -column "SELECT id, summary, priority, domain, assignee, github_pr FROM tasks WHERE status = 'In Progress'"
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
