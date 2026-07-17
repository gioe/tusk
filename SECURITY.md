# Security Policy

## Report a Vulnerability Privately

Please do not disclose a suspected vulnerability in a public issue, discussion, pull request, or task record.

Use GitHub's private vulnerability reporting flow:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Describe the affected version, impact, reproduction steps, and any suggested mitigation.

Include only the minimum data needed to reproduce the issue. Do not attach real task databases, agent transcripts, credentials, tokens, or proprietary project content.

If GitHub's private reporting control is unavailable, contact the repository owner through their GitHub profile and ask for a private reporting channel. Do not include vulnerability details in the initial public message.

## What to Expect

The maintainer will acknowledge the report, assess its impact, and coordinate remediation and disclosure through the private report. Timelines depend on severity and reproducibility; please allow time for a fix and release before publishing details.

## Security-Relevant Areas

Reports are especially useful when they concern:

- shell argument handling or command injection;
- secrets or sensitive content written to logs, task records, or generated artifacts;
- unsafe Git operations or task-worktree boundary escapes;
- path traversal during installation, upgrade, manifest handling, or file materialization;
- unauthorized GitHub operations;
- SQLite corruption, destructive migrations, or cross-project database routing; or
- untrusted content crossing from issues, task descriptions, prompts, or reviews into executable commands.

Tusk is local-first, but it intentionally invokes project test commands, Git, GitHub CLI operations, upgrade downloads, and agent workflows. Treat configuration and executable criterion specifications as code, review changes before running them, and never commit secrets to the task database or repository.
