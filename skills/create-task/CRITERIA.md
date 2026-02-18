## Step 5b: Generate Acceptance Criteria

For each successfully inserted task, generate **3–7 acceptance criteria** using the task's summary, description, and the original source text that informed it. Criteria should be concrete, testable conditions that define "done" for the task.

### How to derive criteria

- Start from the task's **description** — each distinct requirement or expected behavior maps to a criterion
- Add any implicit quality expectations (e.g., error handling, edge cases, validation) if the task type warrants it
- For **bug** tasks, include a criterion that the specific failure case is resolved
- For **feature** tasks, include criteria for the happy path and at least one edge case
- Keep each criterion to a single sentence — actionable and verifiable

### Insert criteria

For each criterion, run:

```bash
tusk criteria add <task_id> "<criterion text>"
```

Use the task ID returned from the INSERT in Step 5. Example for a task with ID 14:

```bash
tusk criteria add 14 "POST /auth/login returns a JWT token for valid credentials"
tusk criteria add 14 "Invalid credentials return 401 with error message"
tusk criteria add 14 "Refresh token endpoint issues a new JWT"
tusk criteria add 14 "Tokens expire after the configured TTL"
```

### Skip criteria for duplicates

If a task was skipped as a duplicate in Step 5, do not generate criteria for it.
