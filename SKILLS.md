# Creating a New Skill

Reference for adding skills to the tusk source repo. See `CLAUDE.md` for the quick checklist.

---

## Directory Structure

Each skill lives in its own directory under `skills/` (source) and gets installed to `.claude/skills/` in target projects:

```
skills/
  my-skill/
    SKILL.md          # Required — main entry point
    REFERENCE.md      # Optional — companion file loaded on demand
```

## SKILL.md Format

Every `SKILL.md` must start with YAML frontmatter:

```yaml
---
name: my-skill
description: One-line description shown in the skill picker
allowed-tools: Bash, Read, Edit     # Comma-separated list of tools the skill needs
---
```

- **`name`**: Must match the directory name, use lowercase kebab-case
- **`description`**: Appears in the Claude Code skill list — keep it concise and action-oriented
- **`allowed-tools`**: Only request tools the skill actually uses. Common sets:
  - Read-only skills: `Bash`
  - Skills that modify files: `Bash, Read, Write, Edit`
  - Skills that search the codebase: `Bash, Read, Glob, Grep`

## Naming Conventions

- Directory and `name` field: lowercase kebab-case (e.g., `check-dupes`, `create-task`)
- The skill is invoked as `/name` (e.g., `/check-dupes`)

## Skill Body Guidelines

- Start with a `# Title` heading after the frontmatter
- Use `## Step N:` headings for multi-step workflows
- Include `bash` code blocks showing exact `tusk` commands to run
- Always use `tusk` CLI for DB access, never raw `sqlite3`
- Use `$(tusk sql-quote "...")` in any SQL that interpolates variables
- Reference other skills by name when integration points exist (e.g., "Run `/check-dupes` before inserting")

## Companion Files

Skills can include additional files beyond `SKILL.md` for reference content that doesn't need to be in the hot path. `install.sh` copies all files in the skill directory, so companion files are automatically available in target projects.

**When to use companion files:**
- The skill has subcommands or detailed reference that would bloat `SKILL.md`
- Content is only needed conditionally (e.g., a specific subcommand is invoked)

**How to reference them:** Use `Read file:` with the `<base_directory>` variable shown at the top of every loaded skill:

```
Read file: <base_directory>/SUBCOMMANDS.md
```

**Example:** The `/tusk` skill uses `SKILL.md` for the default workflow and `SUBCOMMANDS.md` for auxiliary subcommands (`done`, `view`, `list`, etc.), loaded only when needed.

## Source Repo Skill Symlinks

In the tusk source repo, `.claude/skills/` is a **real directory** containing per-skill symlinks. There are two source directories:

- **`skills/`** (public) — Skills distributed to target projects via `install.sh`. Each subdirectory gets a symlink `.claude/skills/<name> → ../../skills/<name>`.
- **`skills-internal/`** (private) — Dev-only skills available in the source repo but **never installed** to target projects. Each subdirectory gets a symlink `.claude/skills/<name> → ../../skills-internal/<name>`.

Run `tusk sync-skills` to regenerate all symlinks after adding or removing a skill directory. The `.gitignore` entry `.claude/skills/` ensures the symlinks themselves are not tracked.

**Editing and staging rules:**

- **Edit only under `skills/` or `skills-internal/`** — editing `.claude/skills/` directly can cause "file modified since read" errors since those are symlinks.
- **Stage only `skills/` or `skills-internal/` paths** — `git add .claude/skills/...` won't work. Always use `git add skills/<name>/SKILL.md` or `git add skills-internal/<name>/SKILL.md`.

Target projects that install tusk get real copies (not symlinks) of `skills/` only — `skills-internal/` is never distributed.
