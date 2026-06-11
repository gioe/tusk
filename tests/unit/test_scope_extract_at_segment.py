"""Regression tests for scope extraction token shapes from issue #1047.

Shapes covered:
1. Literal @ path segment (apps/web/@/components/ui/checkbox.tsx) — _PATH_RE
   used to truncate everything up to and including the @ segment.
2. Brace-expansion lists under an @ directory — _BRACED_PATH_RE used to drop
   the prefix before the @ segment.
3. Bare filename:line[-range] citations (ShowRowTests.swift:243) — resolved
   via extract_referenced_basenames + the unique-basename resolver; pinned
   here against a scratch git repo.
Plus guards that bracket segments (issue #1030) and plain relative paths
keep extracting unchanged.
"""

import importlib.util
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO_ROOT, "bin")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh = _load("tusk_git_helpers_at_segment", "tusk-git-helpers.py")
ti = _load("tusk_task_insert_at_segment", "tusk-task-insert.py")


def test_at_segment_path_extracts_with_full_prefix():
    paths = gh.extract_paths("apps/web/@/components/ui/checkbox.tsx is dead")
    assert "apps/web/@/components/ui/checkbox.tsx" in paths


def test_at_segment_brace_list_expands_with_full_prefix():
    text = (
        "apps/web/@/components/ui/{checkbox,command,dialog}.tsx is a dead "
        "directory; also touches apps/web/components.json"
    )
    candidates = ti._auto_scope_candidates(text)
    assert "apps/web/@/components/ui/checkbox.tsx" in candidates
    assert "apps/web/@/components/ui/command.tsx" in candidates
    assert "apps/web/@/components/ui/dialog.tsx" in candidates
    assert "apps/web/components.json" in candidates
    # The pre-fix symptom: prefix truncated at the @ segment.
    assert "components/ui/checkbox.tsx" not in candidates


def test_bare_filename_line_citation_resolves_via_basename(tmp_path):
    repo = tmp_path / "repo"
    tests_dir = repo / "App" / "Tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "ShowRowTests.swift").write_text("// test\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )

    candidates = ti._auto_scope_candidates(
        "ShowRowTests.swift:243 and :280 flake", repo_root=str(repo)
    )
    assert "App/Tests/ShowRowTests.swift" in candidates


def test_bracket_segments_keep_extracting():
    paths = gh.extract_paths("Fix in apps/web/app/api/v1/comedians/[id]/route.ts")
    assert "apps/web/app/api/v1/comedians/[id]/route.ts" in paths


def test_plain_relative_paths_keep_extracting():
    paths = gh.extract_paths("Edit src/lib/utils.py and tests/test_utils.py")
    assert "src/lib/utils.py" in paths
    assert "tests/test_utils.py" in paths
