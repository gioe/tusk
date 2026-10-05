# Tusk implementation workflow — compact candidate

Use the real project Tusk CLI and task record as the durable source of truth.
Read the assigned task, acceptance criteria, dependencies, and any progress.
Inspect project configuration and task-relevant conventions before changing code.

Explore relevant implementation and tests before choosing a fix. Delegate a
bounded exploration pass to a sub-agent; wait for its findings before choosing
whether to implement locally or delegate implementation. A focused small task
can be implemented locally; delegate larger or ambiguous work with clear scope.

For a bug, add a behavioral regression test and observe the relevant failure
before changing production code. Preserve existing tests. Fix the root cause
without unrelated changes, then run the regression and relevant existing tests.
Distinguish pre-existing failures from regressions and never report skipped,
unexecuted, or failing checks as passing. Ask only when missing information
prevents a correct implementation; proceed on clear, already-authorized work.

Declare intended file scope with reasons through the Tusk CLI before committing;
respect locked scope and explicitly expand it when new work is necessary.
Use task-aware commits with explicit file paths, keep criteria and progress
accurate, and record verification evidence plus remaining work for handoff.
Review the actual diff for correctness and regressions; fix substantive findings
and verify changes. Do not claim a task complete until its required work and
verification are complete. Preserve user files and concurrent changes.

Read only the relevant section of the adjacent `tusk-recovery.md` if exploration,
implementation, deliverable verification, commit, or merge fails. Do not load
the whole recovery companion during normal startup. Follow the coordinator's
explicit phase boundary for this replay; it owns release and finalization.
