# /retro tool-error detection

Design note for TASK-109: how `/retro` surfaces session-scoped tool failures (non-zero Bash exits, Edit/Read/Write rejections, sub-agent errors, ExitPlanMode rejections, etc.) without adding a new PostToolUse hook.

## Decision

`/retro` reads tool-error data **directly from Claude Code transcripts** — the same `~/.claude/projects/<project-hash>/*.jsonl` files `tusk call-breakdown` already parses for per-tool cost attribution. **No PostToolUse hook, no `.claude/session-errors/*.log` sidecar file, and no new install step are required.**

## Why transcripts are sufficient

Every failing tool invocation is already recorded in the transcript as a user-typed `tool_result` block with `is_error: true`. Audit of 20 recent sessions in this project (25 error events):

| Tool          | Errors | Shape of the transcript payload                                                           |
|---------------|-------:|--------------------------------------------------------------------------------------------|
| Bash          | 14     | `content` begins with `Exit code N` followed by the command's stdout/stderr.              |
| Edit          |  4     | `<tool_use_error>File has not been read yet…</tool_use_error>` (and similar guard errors). |
| Read          |  2     | `<tool_use_error>File content exceeds maximum allowed tokens…</tool_use_error>`.          |
| ExitPlanMode  |  2     | `The user doesn't want to proceed with this tool use…` plus the user's feedback text.     |
| Agent         |  1     | `<tool_use_error>Cancelled: parallel tool call Bash(…) errored</tool_use_error>`.         |

Every failure mode called out by TASK-109's description (non-zero Bash exits, Edit/Write failures, sub-agent errors) is already in the transcript — alongside several the hook proposal did not anticipate (Read size limits, ExitPlanMode rejections). Because the `is_error` flag is authoritative for the tool framework, we never have to reconstruct exit codes out-of-band.

## Why not a hook anyway

Adding a PostToolUse hook would duplicate data Claude Code already writes to disk and introduce three new failure modes:

1. **Install drift** — the hook would have to be opted in via `/tusk-update`. Users who installed tusk before the hook existed would silently get no error data in their retros until they ran the updater.
2. **Log-file lifecycle** — per-session `.log` files would need rotation/pruning, a bounded-write contract (criterion 481 called out 200 lines / 32 KB), and cleanup when sessions finish abnormally. Transcripts are already managed by Claude Code and rotated by UUID.
3. **Two sources of truth** — if the hook ever fell out of sync with the transcript (different event ordering, different redaction rules, different truncation), `/retro` authors would have to reason about which one was correct.

## Implementation

- `bin/tusk-pricing-lib.py` gains `iter_tool_errors(transcript_path, started_at, ended_at)` — a sibling to the existing `iter_tool_call_costs` iterator. It single-passes the JSONL, maintains a `tool_use_id → tool_name` map from assistant messages, and yields one dict per user-typed `tool_result` with `is_error: true`.
- `bin/tusk-retro-signals.py` gains a `fetch_tool_errors(conn, task_id)` helper. It looks up every `task_sessions` row for the task, reads each transcript once, and aggregates errors by `tool_name` (error count plus one short sample) — the same shape `/retro` already consumes for `review_themes`.
- `skills/retro/FULL-RETRO.md` renders an "Errors encountered" section in Step 4, modeled on the existing "Session Shape" soft-warning block: omitted silently when the array is empty; no auto-task creation; no promotion to a Proposed Action.

## Out of scope (criteria 462, 463, 481)

These criteria describe the hook path that this evaluation concluded was unnecessary:

- **462** "PostToolUse hook appends tool failures to `.claude/session-errors/<session_id>.log`" — no hook is added; no log file is written.
- **463** "Hook is installed opt-in via `/tusk-update`, not shipped in `config.default.json` by default" — no hook to install.
- **481** "Session error log is bounded (200 lines or 32 KB, whichever comes first)" — no log file to bound; transcript size is managed by Claude Code.

All three are marked skipped on TASK-109 with a pointer back to this note.
