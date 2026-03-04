# Tusk Init Reference: CLAUDE.md Snippet & TODO Seeding

## Step 7: CLAUDE.md Append Workflow

Offer to append a Task Queue section:

> I can add a Task Queue section to your CLAUDE.md with tusk usage instructions. Would you like me to append it?

If the user confirms, append this snippet to the end of `CLAUDE.md`:

```markdown

## Task Queue

The project task database is managed via `tusk`. Use it for all task operations:

\```bash
tusk "SELECT ..."          # Run SQL
tusk -header -column "SQL"  # With formatting flags
tusk path                   # Print resolved DB path
tusk config                 # Print project config
tusk init                   # Bootstrap DB (new projects)
tusk shell                  # Interactive sqlite3 shell
tusk version                # Print installed version
tusk upgrade                # Upgrade from GitHub
\```

Never hardcode the DB path — always go through `tusk`.

Do not modify `.claude/bin/` or `.claude/skills/` — changes are overwritten by `tusk upgrade`.

Found a bug in tusk? Open an issue at https://github.com/gioe/tusker/issues
```

## Step 8: TODO Seeding Workflow

Present found TODOs grouped by file:

> I found **12 TODO/FIXME comments** across your codebase:
>
> **src/api/auth.ts** (3 items):
> - Line 42: `// TODO: Add rate limiting to login endpoint`
> - Line 87: `// FIXME: Token refresh race condition`
> - Line 103: `// TODO: Support OAuth providers`
>
> **src/components/Dashboard.tsx** (2 items):
> - Line 15: `// TODO: Add loading skeleton`
> - Line 89: `// HACK: Workaround for stale cache`
>
> Would you like me to create tasks from any of these? You can say "all", list specific ones, or "skip".

### For each TODO the user wants to keep:

1. Determine task properties:
   - **Summary**: Clean text from the comment (strip `TODO:`, `FIXME:`, etc. prefix)
   - **Domain**: Infer from file path using the configured domains
   - **Priority**: `"High"` for FIXME/HACK, `"Medium"` for TODO/XXX
   - **Task type**: `"bug"` for FIXME/HACK, `"feature"` for TODO/XXX

2. Insert (dupe check is built-in; exit 1 = duplicate — skip and report "Skipped — similar to existing task #N"):
   ```bash
   tusk task-insert "<summary>" "Found in <file>:<line>

   Original comment: <full comment text>" \
     --priority "<priority>" --domain "<domain>" --task-type "<task_type>"
   ```

After all inserts, show a summary:

> **Seeded N tasks** from TODO comments (M skipped as duplicates).
