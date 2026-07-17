# Contributing to Tusk

Thanks for helping improve Tusk. Contributions are most useful when they preserve the project's core promise: durable, auditable context handoff for coding agents without unnecessary infrastructure or token cost.

Before proposing a substantial change, read [the product pillars](docs/PILLARS.md). They are the shared vocabulary for evaluating tradeoffs.

## Report a Problem

Search [existing issues](https://github.com/gioe/tusk/issues) before opening a new one.

If the problem occurred in a project where Tusk is installed, use the [Tusk instance feedback form](https://github.com/gioe/tusk/issues/new?template=tusk-instance-feedback.yml). Include:

- the output of `tusk version`;
- relevant project and agent context without confidential data;
- exact reproduction steps;
- the observed and expected behavior; and
- a runnable command that demonstrates the failure when possible.

Security vulnerabilities should not be filed as public issues. Follow [SECURITY.md](SECURITY.md) instead.

## Develop Locally

Clone the source repository and install the development dependency:

```bash
git clone https://github.com/gioe/tusk.git
cd tusk
python3 -m pip install -r requirements-dev.txt
```

There is no build step or external linter. The source of truth is `bin/tusk`; installed copies under consumer projects are generated artifacts and should not be edited directly.

## Make a Focused Change

- Keep each change independently reviewable.
- Add or update acceptance criteria and tests for changed behavior.
- Preserve existing command output contracts unless the change deliberately revises them.
- Update public docs when a user-facing command, workflow, schema concept, or installation behavior changes.
- Add schema changes through a numbered migration and follow the checklist in [docs/MIGRATIONS.md](docs/MIGRATIONS.md).
- Keep agent-facing output compact. Tusk treats CLI, hook, skill, and prompt output as part of its token budget.

Repository collaborators should use Tusk's task workflow and task-owned worktrees. External contributors may use a conventional fork and topic branch; maintainers will connect accepted work to the appropriate task history.

## Run Tests

Run the focused unit suite during development:

```bash
python3 -m pytest tests/unit/ -q
```

Run the integration suite for changes that affect CLI dispatch, installation, Git behavior, database persistence, migrations, or end-to-end workflows:

```bash
python3 -m pytest tests/integration/ -q
```

The integration suite is intentionally comprehensive and may take significantly longer than the unit suite. It is also run by GitHub Actions on pull requests targeting `main`.

## Open a Pull Request

In the pull request description:

- explain the user-visible problem and the chosen approach;
- identify the product pillar or pillars the change serves;
- list the verification you ran;
- call out schema, migration, installation, or compatibility effects; and
- include screenshots for dashboard or other visual changes.

Small, well-scoped pull requests are easier to verify and merge. A maintainer may ask to split bundled changes when they represent independently shippable work.
