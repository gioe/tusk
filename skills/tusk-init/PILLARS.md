# Tusk Init Reference: Design Pillar Catalogue

Loaded from `SKILL.md` Step 3.5 only when the Pre-check directs you here (either the project has no pillars yet, or the user chose **add more** / **replace all**).

## Resolve `project_type`

Determine `project_type` by checking in order:

1. **Existing config** — run `tusk config project_type`; if it returns a non-empty value, use it.
2. **Step 2e interview result** — use the value derived during the fresh-project interview.
3. **Fallback** — treat as `null` / other if neither source yields a value.

## Suggested pillars (keyed by `project_type`)

| project_type | Suggested pillars |
|---|---|
| `web_app` | Performance, Accessibility, Reliability, Security, Maintainability |
| `ios_app` / `mobile` | Performance, Accessibility, Privacy, Reliability, Ergonomics |
| `python_service` / API | Reliability, Observability, Security, Performance, Maintainability |
| `cli_tool` | Ergonomics, Reliability, Portability, Efficiency, Transparency |
| `data_pipeline` / ML | Reliability, Data Integrity, Observability, Efficiency, Reproducibility |
| `library` | Ergonomics, Stability, Correctness, Portability |
| `docs_site` | Clarity, Discoverability, Accuracy, Maintainability |
| `monorepo` / `null` / other | Reliability, Maintainability, Security, Performance |

## Default core claim per pillar

Use these as the pre-populated claim when presenting each pillar; the user can edit any claim before insertion.

| Pillar | Default core claim |
|---|---|
| Performance | The system responds quickly and uses resources efficiently |
| Accessibility | The product is usable by people of all abilities |
| Reliability | The system behaves correctly and recovers gracefully from failure |
| Security | User data and system resources are protected from unauthorized access |
| Maintainability | The codebase is easy to understand, change, and extend |
| Observability | The system's internal state is visible through logs, metrics, and traces |
| Privacy | User data is collected minimally and handled with care |
| Ergonomics | The interface feels natural and reduces cognitive load for its users |
| Portability | The system runs consistently across environments and platforms |
| Efficiency | The system accomplishes its goals with minimal waste of time or resources |
| Transparency | The system's behavior and reasoning are legible to its users |
| Stability | Public interfaces change rarely and only with clear migration paths |
| Correctness | The system produces accurate results that match its specification |
| Reproducibility | Given the same inputs, the system produces the same outputs every time |
| Data Integrity | Data is accurate, consistent, and never silently corrupted |
| Clarity | Content is easy to understand on first read |
| Discoverability | Users can find what they need without prior knowledge |
| Accuracy | Content reflects the current state of the system without gaps or errors |

## Presentation template

Present the suggested list in a single message:

> **Design Pillars** — these guide tradeoff decisions throughout the project (e.g., "we chose X over Y because of *Reliability*").
>
> Suggested pillars for your project type:
>
> 1. **Performance** — "The system responds quickly and uses resources efficiently"
> 2. **Reliability** — "The system behaves correctly and recovers gracefully from failure"
> 3. ...
>
> Options: **confirm all** · **remove** (e.g., "remove 2") · **edit a claim** (e.g., "edit 1: new claim text") · **add** (e.g., "add Simplicity: ...") · **skip**

Wait for the user's response and apply their edits to the in-memory list before returning to SKILL.md's Insertion sub-step.
