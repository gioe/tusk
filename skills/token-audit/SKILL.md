---
name: token-audit
description: Analyze skill token consumption and surface optimization opportunities
allowed-tools: Bash
---

# Token Audit

Scans all skill directories and reports token consumption across five categories: size census, companion file analysis, SQL anti-patterns, redundancy detection, and narrative density.

## Usage

```bash
# Full human-readable report
tusk token-audit

# Top-level stats + top 5 offenders
tusk token-audit --summary

# Machine-readable JSON
tusk token-audit --json
```

## Interpreting Results

- **Size Census**: Skills ranked by total lines (SKILL.md + companions). Estimated tokens use ~10 tokens/line.
- **Companion Analysis**: `UNCONDITIONAL` loads inject tokens on every invocation — consider adding conditional guards.
- **SQL Anti-Patterns**: `WARN` items (SELECT *) pull unnecessary columns. `INFO` items are advisory.
- **Redundancy**: Duplicate `tusk` commands or `tusk setup` followed by `tusk config`/`tusk conventions` re-fetches.
- **Narrative Density**: Prose:code ratio > 3.0 suggests the skill could benefit from more code examples or prose trimming.
