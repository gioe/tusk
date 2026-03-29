---
name: investigate-directory
description: Audit a directory's purpose and alignment with the tusk client project — reads contents, queries pillars from DB, cross-references root CLAUDE.md, and gives an honest assessment
allowed-tools: Bash, Read, Glob, Grep
---

# Investigate Directory Skill

Reads a directory's full file tree, loads project context (pillars from DB + root CLAUDE.md), and delivers an honest, unstructured assessment of whether the directory is serving its purpose relative to the tusk client project.

**This skill is read-only — it never modifies files or creates tasks.**

## Step 0: Start Cost Tracking

```bash
tusk skill-run start investigate-directory
```

Capture `run_id` from the output — needed in Step 6.

## Step 1: Capture the Directory Argument

The user provides a directory path after `/investigate-directory`. It may be:
- An absolute path (e.g., `/Users/me/project/src/auth`)
- A path relative to the project root (e.g., `src/auth`, `skills/tusk`)

If no argument was provided, ask:

> Which directory should I investigate? Provide a path relative to the project root (e.g., `src/auth`) or an absolute path.

**Resolve to an absolute path:**

```bash
tusk path        # prints the path to tasks.db
```

The project root is the parent of the `tusk/` directory returned by `tusk path`. If the argument is relative, prepend the project root. If it is already absolute, use it as-is. Confirm the directory exists before proceeding.

## Step 2: Load Project Context

Run these in parallel — hold all results in context for Step 4.

```bash
tusk setup       # returns config JSON + open backlog
```

Then fetch pillars and read project context in parallel:

```bash
tusk pillars list   # returns [{id, name, core_claim}] or [] if none defined
```

```
Read file: <project_root>/CLAUDE.md   # project purpose, architecture, and key conventions
```

Also fetch any directory-related conventions:

```bash
tusk conventions search directory
```

If the pillars array is empty, note it — the assessment will rely on CLAUDE.md and config alone.

## Step 3: Read the Target Directory

### 3a — Full recursive file enumeration (always run)

```bash
find <directory> -type f | sort
```

This runs every time regardless of directory depth. It is the foundation of the assessment — the complete file tree reveals scope, structure, and potential drift.

**If the tree has more than 100 files**, note the total count and group by subdirectory and extension rather than listing every file. Identify the clusters that matter most for understanding purpose.

### 3b — Selective content reading (based on what 3a revealed)

Read files in priority order:

1. `<directory>/CLAUDE.md` — if present, read in full. This is the authoritative statement of the directory's intended purpose.
2. Any `README`, `README.md`, or top-level docs files — read in full.
3. 2–5 key source files that represent core behavior — choose based on the file tree (entry points, main modules, primary config files, index files).
4. If the directory has subdirectories, note their names — structure often signals intent.

Do not read every file. The goal is to understand purpose, not enumerate line counts.

## Step 4: Analyze

Answer these questions from the evidence gathered:

| Question | Why it matters |
|----------|----------------|
| What does this directory actually do? | Derived from the code and docs you read |
| What was it intended to do? | From its CLAUDE.md, root CLAUDE.md references, or tusk config |
| Which project pillar(s) does it serve? | Maps the directory to the product's stated values |
| Is the content aligned with its stated or inferred purpose? | The core alignment question |
| Are there signs of scope creep, dead code, or missing pieces? | Drift detection |
| Does the directory boundary make sense? | Could it be merged with a sibling, or should it be split? |
| Are there open backlog tasks already addressing issues here? | Prevents re-surfacing known work |

## Step 5: Output Assessment

There is no required format — the output is intentionally unstructured. A good assessment includes:

- **What this directory does** — a plain-language description drawn from Step 3
- **Its relationship to the project** — how it fits into the overall architecture described in CLAUDE.md
- **Which pillar(s) it serves** — from `tusk pillars list` (or note if pillars array was empty)
- **An honest verdict** — one of:
  - *Serving its purpose well* — aligned, coherent, no significant gaps
  - *Needs work* — gaps, drift, or misalignment found; describe what
  - *Unclear* — stated purpose is absent or vague; describe what's missing
  - *Good as-is* — no action warranted
- **Specific observations** — anything concrete that supports the verdict (file names, patterns, missing files, scope creep signals)

Keep the assessment honest. If the directory is fine, say so directly. If it has problems, name them specifically.

## Step 6: Finish Cost Tracking

```bash
tusk skill-run finish <run_id> --metadata '{}'
```
