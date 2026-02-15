---
name: lint-conventions
description: Check codebase for violations of tusk project conventions
allowed-tools: Bash, Grep, Glob, Read
---

# Lint Conventions

Checks the tusk codebase against Key Conventions from CLAUDE.md. Each convention with a detectable anti-pattern has a grep-based rule. Run this before releasing or as a pre-PR sanity check.

## How It Works

Run every rule below in order. Collect violations as `file:line: description` entries. At the end, print a summary and exit with status 1 if any violations were found.

**Important**: Some files are legitimately exempt from certain rules (noted per-rule). Skip those files when grepping. Always exclude `skills/lint-conventions/SKILL.md` itself from results — it contains anti-patterns as examples in its rule definitions.

## Rules

### Rule 1: No raw `sqlite3` usage

**Convention**: All DB access goes through `bin/tusk`, never raw `sqlite3`.

Search for direct `sqlite3` invocations outside of `bin/tusk` itself:

```bash
grep -rn 'sqlite3 ' skills/ scripts/ --include='*.md' --include='*.sh' --include='*.py' | grep -v 'bin/tusk' | grep -v '^\s*#'
```

**Exempt files**: `bin/tusk` (it wraps sqlite3), `CLAUDE.md` (documentation).

Flag any match as a violation.

### Rule 2: SQL `!=` operator in bash/skill contexts

**Convention**: In SQL passed through bash, use `<>` instead of `!=` — the `!` can trigger shell history expansion.

Search for `!=` inside SQL code blocks in skills and shell scripts:

```bash
grep -rn "!=" skills/ bin/ scripts/ --include='*.md' --include='*.sh' | grep -vi 'claude\.md' | grep -vi 'README'
```

**Exempt files**: `CLAUDE.md`, `README.md`, Python files (Python `!=` is fine).

Review each match: only flag lines that are clearly inside SQL statements (WHERE clauses, HAVING, CHECK constraints). Ignore bash conditionals like `[[ "$x" != "y" ]]` or Python comparisons.

### Rule 3: Hardcoded database path

**Convention**: Never hardcode the database path. Use `tusk path` or `bin/tusk` to resolve it.

```bash
grep -rn 'tusk/tasks\.db' skills/ scripts/ bin/ --include='*.md' --include='*.sh' --include='*.py'
```

**Exempt files**: `CLAUDE.md`, `README.md`, `install.sh` (sets up the path), `bin/tusk` (defines the path).

Flag any match in non-exempt files.

### Rule 4: Manual single-quote escaping instead of `tusk sql-quote`

**Convention**: Use `$(tusk sql-quote "...")` to safely escape user-provided text — never manually escape single quotes.

Look for manual quote-escaping patterns in SQL contexts:

```bash
grep -rn "sed.*s/'/" skills/ scripts/ --include='*.md' --include='*.sh' | grep -v 'sql_quote'
```

Also check for inline doubled-quote patterns like `''` used for escaping outside of `tusk sql-quote`:

```bash
grep -rn "replace.*'.*''" skills/ scripts/ --include='*.md' --include='*.sh' --include='*.py' | grep -v 'sql.quote'
```

**Exempt files**: `bin/tusk` (contains the `sql_quote()` implementation).

Flag any match in non-exempt files.

### Rule 5: Setting status='Done' without closed_reason

**Convention**: Must set `closed_reason` when marking a task Done.

```bash
grep -rn "status.*=.*'Done'" skills/ scripts/ bin/ --include='*.md' --include='*.sh' --include='*.py'
```

For each match, check whether `closed_reason` appears in the same SQL statement (same line or adjacent lines in the same code block). Flag lines where `status = 'Done'` is set without a corresponding `closed_reason` assignment.

## Output Format

Print results grouped by rule:

```
=== Lint Conventions Report ===

Rule 1: No raw sqlite3 usage
  PASS — no violations

Rule 2: SQL != operator
  WARN — 2 violations
  skills/example/SKILL.md:45: WHERE status != 'Done'
  scripts/cleanup.sh:12: AND priority != 'Low'

Rule 3: Hardcoded database path
  PASS — no violations

Rule 4: Manual quote escaping
  PASS — no violations

Rule 5: Done without closed_reason
  PASS — no violations

=== Summary: 2 violations across 1 rule ===
```

Use `PASS` for rules with no violations and `WARN` for rules with violations.

## Adding New Rules

To add a rule, append a new `### Rule N` section with:
1. The convention being checked (quote from CLAUDE.md)
2. The grep command to detect the anti-pattern
3. Exempt files (if any)
4. Guidance on what constitutes a true positive vs false positive

## Integration Points

This skill can be referenced by:
- **`/next-task`** — As an optional pre-PR check
- **`/retro`** — To audit convention compliance after a session
