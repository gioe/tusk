---
name: investigate-directory
description: "Audit a directory: (1) Primary — describe its actual purpose from content, structure, and project context; (2) Secondary — assess code quality signals and pillar alignment, including how well it serves its stated values"
allowed-tools: Bash, Read, Glob, Grep
---

# Investigate Directory Skill

**Primary output:** A plain-language description of what the directory actually does — derived from its file tree, content, and project context — so the caller understands its real purpose, not just its label. **Secondary output:** An assessment of code quality signals (naming consistency, test coverage, dead files, convention adherence) and how well the directory serves the project's pillar(s) — not just which pillars apply, but whether the implementation lives up to them.

**This skill is read-only — it never modifies files or creates tasks.**

## Step 0: Start Cost Tracking

```bash
tusk skill-run start investigate-directory
```

Capture `run_id` from the output — needed in Step 6.

> **Early-exit cleanup:** If any check below causes the skill to stop before Step 6 (e.g., the user never provides a directory in Step 1, the resolved path does not exist, or `tusk setup` / `tusk pillars list` fails), first call `tusk skill-run cancel <run_id>` to close the open row, then stop. Otherwise the row lingers as `(open)` in `tusk skill-run list` forever.

## Step 1: Capture the Directory Argument

The user provides a directory path after `/investigate-directory`. It may be:
- An absolute path (e.g., `/Users/me/project/src/auth`)
- A path relative to the project root (e.g., `src/auth`, `skills/tusk`)

If no argument was provided, ask:

> Which directory should I investigate? Provide a path relative to the project root (e.g., `src/auth`) or an absolute path.

If the user does not respond, or declines to provide a path, run `tusk skill-run cancel <run_id>` and stop. This closes the open `skill_runs` row instead of leaving it pending forever.

**Resolve to an absolute path:**

```bash
tusk path        # prints the path to tasks.db
```

The project root is the parent of the `tusk/` directory returned by `tusk path`. If the argument is relative, prepend the project root. If it is already absolute, use it as-is. Confirm the directory exists before proceeding — if it does not, run `tusk skill-run cancel <run_id>` and stop with "Directory `<path>` not found."

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

Before answering the questions below, fetch domain-specific conventions:

```bash
tusk conventions search <domain>   # use the directory's domain or its inferred purpose as the topic
```

Answer these questions from the evidence gathered:

| Question | Why it matters |
|----------|----------------|
| What does this directory actually do? | Derived from the code and docs you read |
| What was it intended to do? | From its CLAUDE.md, root CLAUDE.md references, or tusk config |
| Which project pillar(s) does it serve, and how well? | Maps the directory to the product's stated values and judges quality of fit — not just presence |
| Is the content aligned with its stated or inferred purpose? | The core alignment question |
| Are there signs of scope creep, dead code, or missing pieces? | Drift detection |
| Does the directory boundary make sense? | Could it be merged with a sibling, or should it be split? |
| Are there open backlog tasks already addressing issues here? | Prevents re-surfacing known work |
| Code quality signals: naming consistency, test coverage indicators, stale/dead files, convention adherence | Surface concrete quality signals that may not appear in pillar or purpose analysis |

## Step 5: Output Assessment

Structure the output in two labeled sections. Within each section the format is intentionally unstructured — write prose, bullets, or a mix, whatever communicates the finding clearly.

### Purpose (Primary)

- **What this directory does** — a plain-language description drawn from Step 3
- **What it was intended to do** — from its CLAUDE.md, root CLAUDE.md, or inferred from config
- **Its relationship to the project** — how it fits into the overall architecture described in CLAUDE.md
- **Verdict** — one of:
  - *Serving its purpose well* — aligned, coherent, no significant gaps
  - *Needs work* — gaps, drift, or misalignment found; describe what
  - *Unclear* — stated purpose is absent or vague; describe what's missing
  - *Good as-is* — no action warranted

### Quality & Alignment (Secondary)

- **Which pillar(s) it serves and how well** — from `tusk pillars list`; name the pillar(s) and judge the quality of fit (not just presence). Note if the pillars array was empty.
- **Code quality signals** — concrete observations on: naming consistency, test coverage indicators, stale or dead files, convention adherence (from Step 4 conventions search)
- **Specific observations** — anything concrete that supports the verdict (file names, patterns, missing files, scope creep signals)

Keep the assessment honest. If the directory is fine, say so directly. If it has problems, name them specifically.

## Step 6: Finish Cost Tracking

```bash
tusk skill-run finish <run_id> --metadata '{}'
```
