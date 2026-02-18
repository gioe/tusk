# Tusk Init Reference: Project Description Seeding

## Step 9: Interactive Project Description Seeding

### 9a: Gather Project Description

Ask the user for a high-level description:

> Tell me about your project — what are you building? A few sentences is enough, but share as much detail as you'd like.

### 9b: Clarifying Questions (1-2 rounds)

Based on the description, ask **one round** of 2-4 focused questions covering:

- **Core features vs. nice-to-haves**: Which capabilities are must-haves for an initial version?
- **Target users / key workflows**: Who uses this and what's their primary flow?
- **Known technical constraints**: Specific tech stack, integrations, or deployment requirements?

Ask a **second round only** if major ambiguity remains after the first. If the description was already detailed, skip directly to synthesis.

### 9c: Synthesize Brief

Combine the description and answers into a structured brief (do not show to user — use as decomposition input):

- **Purpose**: One-sentence summary
- **Core features**: Bulleted must-haves
- **Users and workflows**: Who and how
- **Technical constraints**: Stack, integrations, deployment
- **Out of scope**: Nice-to-haves deferred for later

### 9d: Hand Off to Create-Task Pipeline

The synthesized brief replaces user input. Project config is already known from the tusk-init wizard (Steps 3-5).

Read the /create-task skill and follow it **from Step 2b onward**:

```
Read file: <base_directory>/../create-task/SKILL.md
```

Follow Steps 2b through 6, using the synthesized brief as the input text and the confirmed domains/task types/agents as config values.
