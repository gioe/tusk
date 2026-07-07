# Safe Bootstrap Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `tusk init-write-manifest-files` so bootstrap plans can safely dry-run, render intent templates, and update marker-bounded managed sections.

**Architecture:** Keep `bin/tusk-init-write-manifest-files.py` as the single materialization utility. Add a small renderer, explicit conflict reporting, dry-run branching, and a new `marker_block` mode while preserving existing create-only and append-if-missing behavior.

**Tech Stack:** Python standard library, repo-local `bin/tusk` wrapper, pytest integration tests.

---

### Task 1: Pin Missing Writer Behavior With Tests

**Files:**
- Modify: `tests/integration/test_bootstrap_manifest_writer.py`

- [ ] **Step 1: Add failing tests for dry-run, templates, marker blocks, and conflicts**

Add tests that call `_write_manifest` with new `extra_args` support and assert:

```python
def test_bootstrap_manifest_dry_run_reports_writes_without_mutating(project_root):
    spec = [{"path": "generated.txt", "content": "hello\n"}]
    result = _write_manifest(project_root, spec, repo_root=project_root, extra_args=["--dry-run"])
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["wrote"] == [{"path": "generated.txt", "mode": "create_only", "dry_run": True}]
    assert not (project_root / "generated.txt").exists()


def test_bootstrap_manifest_renders_intent_templates(project_root):
    intent_path = project_root / "intent.json"
    intent_path.write_text(json.dumps({"project_name": "Ledger", "init_intent": {"platforms": ["ios"]}}), encoding="utf-8")
    spec = [{"path": "README.md", "content": "# {{ project_name }}\nPlatform: {{ init_intent.platforms.0 }}\n"}]
    result = _write_manifest(project_root, spec, repo_root=project_root, extra_args=["--intent-file", str(intent_path)])
    assert result.returncode == 0
    assert (project_root / "README.md").read_text(encoding="utf-8") == "# Ledger\nPlatform: ios\n"


def test_bootstrap_manifest_marker_block_replaces_only_managed_section(project_root):
    target = project_root / "Package.swift"
    target.write_text("// user header\n// BEGIN TUSK\nold\n// END TUSK\n// user footer\n", encoding="utf-8")
    spec = [{"path": "Package.swift", "content": "new\n", "mode": "marker_block", "begin_marker": "// BEGIN TUSK", "end_marker": "// END TUSK"}]
    result = _write_manifest(project_root, spec, repo_root=project_root)
    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == "// user header\n// BEGIN TUSK\nnew\n// END TUSK\n// user footer\n"


def test_bootstrap_manifest_marker_block_conflict_does_not_mutate_partial_marker(project_root):
    target = project_root / "Package.swift"
    target.write_text("// user header\n// BEGIN TUSK\nold\n", encoding="utf-8")
    spec = [{"path": "Package.swift", "content": "new\n", "mode": "marker_block", "begin_marker": "// BEGIN TUSK", "end_marker": "// END TUSK"}]
    result = _write_manifest(project_root, spec, repo_root=project_root)
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["conflicts"][0]["path"] == "Package.swift"
    assert target.read_text(encoding="utf-8") == "// user header\n// BEGIN TUSK\nold\n"
```

- [ ] **Step 2: Run tests and confirm they fail for missing CLI support**

Run: `python3 -m pytest tests/integration/test_bootstrap_manifest_writer.py -q`

Expected: failures mention missing `--dry-run`, missing `--intent-file`, unsupported `marker_block`, or absent `conflicts`.

### Task 2: Implement Safe Materialization

**Files:**
- Modify: `bin/tusk-init-write-manifest-files.py`

- [ ] **Step 1: Add parser flags and data plumbing**

Add `--dry-run` and `--intent-file`, load intent JSON into a dict, and pass `dry_run` plus `intent` into `_write_one`.

- [ ] **Step 2: Add deterministic template rendering**

Implement `{{ dotted.path }}` replacement for dictionaries and lists. Return a conflict when a template variable is missing.

- [ ] **Step 3: Add conflict-aware operation results**

Return one of `wrote`, `skipped`, `conflict`, or `error` from `_write_one`. Aggregate `conflicts` in the top-level JSON and exit 1 when any conflict is present.

- [ ] **Step 4: Add dry-run branches**

When `dry_run` is true, report would-write entries with `"dry_run": true` and do not call `os.makedirs` or write files.

- [ ] **Step 5: Add marker_block mode**

Require `begin_marker` and `end_marker`. Create a missing file as `begin + content + end`, replace only the bounded section when both markers exist, skip when the rendered bounded section is already present, and conflict when exactly one marker exists or markers are malformed.

- [ ] **Step 6: Run targeted tests**

Run: `python3 -m pytest tests/integration/test_bootstrap_manifest_writer.py -q`

Expected: all tests pass.

### Task 3: Validate Manifest Contract And Documentation

**Files:**
- Modify: `bin/tusk-init-fetch-bootstrap.py`
- Modify: `docs/DOMAIN.md`
- Modify: `docs/SCRIPTS.md`
- Modify: `skills/tusk-init/SKILL.md`
- Modify: `codex-prompts/tusk-init.md`
- Modify: `tests/unit/test_init_fetch_bootstrap_validate.py`

- [ ] **Step 1: Add validation tests for marker_block**

Add unit tests proving `marker_block` passes when both markers are strings and fails when either marker is missing.

- [ ] **Step 2: Extend bootstrap validation**

Add `marker_block` to valid modes and require `begin_marker` / `end_marker` for that mode.

- [ ] **Step 3: Update docs and skill prompt contract**

Document `marker_block`, template variables, dry-run, and conflict reporting wherever the manifest writer is described.

- [ ] **Step 4: Run targeted validation tests**

Run: `python3 -m pytest tests/unit/test_init_fetch_bootstrap_validate.py tests/integration/test_bootstrap_manifest_writer.py -q`

Expected: all tests pass.

### Task 4: Verify And Commit

**Files:**
- All modified files above.

- [ ] **Step 1: Run the unit suite**

Run: `python3 -m pytest tests/unit/ -q`

Expected: all tests pass.

- [ ] **Step 2: Mark TASK-758 criteria done**

Run `TUSK_DB=/Users/mattgioe/Desktop/projects/tusk/tusk/tasks.db ./bin/tusk criteria done <criterion-id>` for criteria 3529 through 3533 after verification.

- [ ] **Step 3: Commit with Tusk**

Run: `TUSK_DB=/Users/mattgioe/Desktop/projects/tusk/tusk/tasks.db ./bin/tusk commit 758 "Implement safe bootstrap file materialization" <changed-files> --criteria 3529 --criteria 3530 --criteria 3531 --criteria 3532 --criteria 3533`

Expected: commit succeeds and links all acceptance criteria.
