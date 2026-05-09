# Issue Cluster Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a required issue-cluster classification path for tusk GitHub issues.

**Architecture:** Keep the cluster taxonomy in the issue filing surfaces rather than adding a new database concept. The GitHub issue template captures manual intent, while `tusk report-issue` validates and applies labels for automated reports.

**Tech Stack:** Bash CLI in `bin/tusk`, GitHub issue forms YAML, Markdown skill instructions, pytest shell tests.

---

### Task 1: Report-Issue Cluster CLI

**Files:**
- Modify: `bin/tusk`
- Create: `tests/integration/test_report_issue_cluster.py`

- [ ] **Step 1: Write failing tests**

Create `tests/integration/test_report_issue_cluster.py` with dry-run tests that assert default, explicit, and invalid cluster handling.

- [ ] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/integration/test_report_issue_cluster.py -v`

Expected: tests fail because `--cluster` is unsupported and dry-run output only shows `instance-feedback`.

- [ ] **Step 3: Implement CLI validation**

In `cmd_report_issue`, add a fixed allowed cluster list, parse `--cluster`, validate it, and pass both labels to dry-run and real `gh issue create`.

- [ ] **Step 4: Run tests and verify they pass**

Run: `python3 -m pytest tests/integration/test_report_issue_cluster.py -v`

Expected: all tests pass.

### Task 2: Manual Template And Skill Guidance

**Files:**
- Modify: `.github/ISSUE_TEMPLATE/tusk-instance-feedback.yml`
- Modify: `skills/report-tusk-issue/SKILL.md`
- Modify: `skills/retro/SKILL.md`
- Modify: `skills/retro/FULL-RETRO.md`
- Modify: `tests/unit/test_address_issue_polarity_check.py`

- [ ] **Step 1: Write failing template/skill assertions**

Add tests that require the template dropdown and the report skill's `--cluster` pass-through.

- [ ] **Step 2: Run tests and verify they fail**

Run: `python3 -m pytest tests/unit/test_address_issue_polarity_check.py -v`

Expected: new assertions fail before the template and skill docs are updated.

- [ ] **Step 3: Update template and skill docs**

Add the required cluster dropdown to the issue template and update the report/retro skills to select and pass `--cluster`.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/unit/test_address_issue_polarity_check.py tests/integration/test_report_issue_cluster.py -v`

Expected: all focused tests pass.

### Task 3: Repository Labels

**Files:**
- GitHub labels only

- [ ] **Step 1: Create missing labels**

Run `gh label create` for the cluster labels that do not already exist.

- [ ] **Step 2: Verify labels**

Run `gh label list --limit 200` and confirm every `cluster:*` label exists.
