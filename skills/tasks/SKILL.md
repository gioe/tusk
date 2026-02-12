---
name: tasks
description: Open the tasks database in DB Browser for SQLite GUI
allowed-tools: Bash
---

# Tasks Skill

Opens the project task database (via `tusk` CLI) in DB Browser for SQLite for visual browsing and querying.

## Usage

When this skill is invoked, run:

```bash
open -a "DB Browser for SQLite" "$(.claude/bin/tusk path)"
```

Then confirm to the user that the database has been opened in DB Browser for SQLite.

## Quick Stats

Before opening, show a quick summary:

```bash
.claude/bin/tusk "SELECT status, COUNT(*) as count FROM tasks GROUP BY status ORDER BY count DESC"
```
