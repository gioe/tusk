"""Regression coverage for task-brief verification paths across chained cd."""

import importlib.util
import os
import shlex

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-task-brief.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_task_brief_chained_cd", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


brief = _load_module()


@pytest.mark.parametrize("python", ["python", "python3", "python3.12", "/usr/bin/python3"])
def test_inline_python_source_does_not_emit_stale_warning(tmp_path, python):
    target = tmp_path / "android/app/src/androidTest/AppStoreScreenshotTest.kt"
    target.parent.mkdir(parents=True)
    target.touch()
    source = f'from pathlib import Path; Path("{target.relative_to(tmp_path)}").read_text()'
    rows = [{"id": 1, "verification_spec": f"{python} -c {shlex.quote(source)}"}]

    assert brief._stale_spec_warnings(str(tmp_path), rows) == []


@pytest.mark.parametrize("flags", ["", "-I -B", "-W ignore -X dev"])
def test_inline_python_keeps_argv_and_chained_shell_paths(flags):
    source = shlex.quote('from pathlib import Path; Path("ignored/source.kt").read_text()')
    assert brief._spec_paths(
        f"python3 {flags} -c {source} missing/argument.kt && "
        "cd apps && cat missing/after.kt"
    ) == ["missing/argument.kt", "apps", "apps/missing/after.kt"]


@pytest.mark.parametrize(
    "command",
    ["python3 script.py", "python3 -m module", "python3 -- script.py", "rg"],
)
def test_non_interpreter_c_option_keeps_real_missing_paths(command):
    assert "missing/file.py" in brief._spec_paths(f"{command} -c missing/file.py")


def test_inline_python_inside_command_substitution_preserves_shell_scan():
    source = shlex.quote('print("ignored/source.kt")')
    assert brief._spec_paths(
        f'cd apps && test "$(python3 -c {source})" && cat missing/after.kt'
    ) == ["apps", "apps/missing/after.kt"]


def test_python_source_substitution_does_not_hide_following_operands():
    assert brief._spec_paths(
        '.venv/bin/python3 -c "$(cat scripts/check.py)" missing/argument.kt'
    ) == [".venv/bin/python3", "scripts/check.py", "missing/argument.kt"]


def test_inline_python_warning_contains_only_real_missing_shell_path(tmp_path):
    source = shlex.quote('print("not/a/path")')
    rows = [{"id": 1, "verification_spec": f"python3 -c {source} && cat missing/file.kt"}]

    warnings = brief._stale_spec_warnings(str(tmp_path), rows)

    assert len(warnings) == 1
    assert warnings[0]["details"]["missing_paths"] == ["missing/file.kt"]

TRENDING_SPEC = (
    "rg -Fq 'prefers an active avatar over legacy image state' "
    "apps/web/lib/data/home/getTrendingComedians.test.ts && "
    "cd apps/web && npm test -- lib/data/home/getTrendingComedians.test.ts "
    "-t 'prefers an active avatar over legacy image state'"
)
ZIP_SPEC = (
    "rg -Fq 'prefers an active avatar over legacy image state' "
    "apps/web/lib/data/home/getComediansByZip.test.ts && "
    "cd apps/web && npm test -- lib/data/home/getComediansByZip.test.ts "
    "-t 'prefers an active avatar over legacy image state'"
)
BOTH_SPEC = (
    "rg -Fq 'resolves active avatars without per-row queries' "
    "apps/web/lib/data/home/getTrendingComedians.test.ts && "
    "rg -Fq 'resolves active avatars without per-row queries' "
    "apps/web/lib/data/home/getComediansByZip.test.ts && "
    "cd apps/web && npm test -- "
    "lib/data/home/getTrendingComedians.test.ts "
    "lib/data/home/getComediansByZip.test.ts "
    "-t 'resolves active avatars without per-row queries'"
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (".venv/bin/python3", ".venv/bin/python3"),
        ("(.venv/bin/python3).", ".venv/bin/python3"),
        ("./scripts/test.sh", "scripts/test.sh"),
    ],
)
def test_clean_path_token_preserves_meaningful_leading_dots(token, expected):
    assert brief._clean_path_token(token) == expected


def test_spec_paths_exclude_xctest_target_and_test_case_selector():
    assert brief._spec_paths(
        "ios/bin/test-sim LaughTrackTests/SoftPushPromptCoordinatorTests"
    ) == ["ios/bin/test-sim"]


@pytest.mark.parametrize(
    "path",
    [
        "apps/web",
        "fixtures/data",
        "Tests/FooTests",
        "TargetTests/fixtures",
        "FooTests/BarTests",
        "ios/Tests/FooTests.swift",
    ],
)
def test_spec_paths_preserve_non_xctest_two_segment_paths(path):
    assert brief._spec_paths(path) == [path]


def test_xctest_shaped_path_is_preserved_for_other_commands():
    assert brief._spec_paths("cat FooTests/BarTests") == [
        "FooTests/BarTests"
    ]


def test_xctest_selector_does_not_emit_stale_warning(tmp_path):
    runner = tmp_path / "ios" / "bin" / "test-sim"
    runner.parent.mkdir(parents=True)
    runner.touch()
    rows = [
        {
            "id": 1,
            "verification_spec": (
                "ios/bin/test-sim "
                "LaughTrackTests/SoftPushPromptCoordinatorTests"
            ),
        }
    ]

    assert brief._stale_spec_warnings(str(tmp_path), rows) == []


def test_xctest_selector_filter_keeps_real_missing_operands(tmp_path):
    runner = tmp_path / "ios" / "bin" / "test-sim"
    runner.parent.mkdir(parents=True)
    runner.touch()
    swift_file = tmp_path / "ios" / "Tests" / "ExistingTests.swift"
    swift_file.parent.mkdir(parents=True)
    swift_file.touch()
    rows = [
        {
            "id": 2,
            "verification_spec": (
                "ios/bin/test-sim "
                "LaughTrackTests/SoftPushPromptCoordinatorTests "
                "ios/Tests/ExistingTests.swift "
                "missing/ExpectedTests.swift"
            ),
        }
    ]

    warnings = brief._stale_spec_warnings(str(tmp_path), rows)

    assert warnings[0]["details"]["missing_paths"] == [
        "missing/ExpectedTests.swift"
    ]


@pytest.mark.parametrize(
    ("verification_spec", "expected_paths"),
    [
        (
            TRENDING_SPEC,
            [
                "apps/web/lib/data/home/getTrendingComedians.test.ts",
                "apps/web",
            ],
        ),
        (
            ZIP_SPEC,
            [
                "apps/web/lib/data/home/getComediansByZip.test.ts",
                "apps/web",
            ],
        ),
        (
            BOTH_SPEC,
            [
                "apps/web/lib/data/home/getTrendingComedians.test.ts",
                "apps/web/lib/data/home/getComediansByZip.test.ts",
                "apps/web",
            ],
        ),
    ],
)
def test_spec_paths_track_cd_after_root_relative_checks(
    verification_spec, expected_paths
):
    assert brief._spec_paths(verification_spec) == expected_paths


def test_exact_laughtrack_specs_do_not_emit_stale_warnings(tmp_path):
    home_dir = tmp_path / "apps" / "web" / "lib" / "data" / "home"
    home_dir.mkdir(parents=True)
    (home_dir / "getTrendingComedians.test.ts").touch()
    (home_dir / "getComediansByZip.test.ts").touch()
    rows = [
        {"id": 12512, "verification_spec": TRENDING_SPEC},
        {"id": 12513, "verification_spec": ZIP_SPEC},
        {"id": 12514, "verification_spec": BOTH_SPEC},
    ]

    assert brief._stale_spec_warnings(str(tmp_path), rows) == []


def test_missing_paths_respect_working_directory_at_each_command(tmp_path):
    (tmp_path / "apps" / "web").mkdir(parents=True)
    rows = [
        {
            "id": 1,
            "verification_spec": (
                "rg -Fq marker missing/root.test.ts && "
                "cd apps/web && npm test -- missing/nested.test.ts"
            ),
        }
    ]

    warnings = brief._stale_spec_warnings(str(tmp_path), rows)

    assert warnings[0]["details"]["missing_paths"] == [
        "missing/root.test.ts",
        "apps/web/missing/nested.test.ts",
    ]


def test_leading_literal_cd_behavior_is_preserved():
    assert brief._spec_paths("cd apps/web && pytest lib/example.test.ts") == [
        "apps/web",
        "apps/web/lib/example.test.ts",
    ]


@pytest.mark.parametrize(
    ("verification_spec", "expected_paths"),
    [
        (
            "cd apps | rg -Fq marker lib/example.test.ts",
            ["apps", "lib/example.test.ts"],
        ),
        (
            "cd apps & rg -Fq marker lib/example.test.ts",
            ["apps", "lib/example.test.ts"],
        ),
        (
            "cd apps ; rg -Fq marker lib/example.test.ts",
            ["apps", "apps/lib/example.test.ts"],
        ),
        (
            "cd apps || rg -Fq marker lib/example.test.ts",
            ["apps", "apps/lib/example.test.ts"],
        ),
        (
            "cd apps && command | rg -Fq marker lib/example.test.ts",
            ["apps", "apps/lib/example.test.ts"],
        ),
        (
            "cd apps && command & rg -Fq marker lib/example.test.ts",
            ["apps", "lib/example.test.ts"],
        ),
    ],
)
def test_spec_paths_respect_shell_cwd_boundaries(
    verification_spec, expected_paths
):
    assert brief._spec_paths(verification_spec) == expected_paths


def test_dot_prefixed_executable_resolves_after_chained_cd():
    assert brief._spec_paths(
        "cd apps/scraper && "
        ".venv/bin/python3 -m pytest tests/unit/test_example.py"
    ) == [
        "apps/scraper",
        "apps/scraper/.venv/bin/python3",
        "apps/scraper/tests/unit/test_example.py",
    ]


def test_spec_paths_extract_file_from_quoted_command_substitution():
    assert brief._spec_paths(
        "test \"$(rg -F '.github/workflows/web-ci.yml' "
        ".github/workflows/web-ci.yml | wc -l | tr -d '[:space:]')\" -ge 3"
    ) == [".github/workflows/web-ci.yml"]


def test_nested_command_substitution_inherits_and_restores_working_directory():
    assert brief._spec_paths(
        "cd apps && test \"$(printf '%s' \"$(cd web && "
        "rg marker tests/inside.test.ts)\")\" && cat tests/after.test.ts"
    ) == [
        "apps",
        "apps/web",
        "apps/web/tests/inside.test.ts",
        "apps/tests/after.test.ts",
    ]


def test_command_substitution_detection_respects_literal_and_escaped_dollars():
    literal = brief._shell_scan_tokens("printf '%s' '$(rg marker missing/fake.py)'")
    escaped = brief._shell_scan_tokens(r"printf '%s' \$(rg marker missing/fake.py)")
    arithmetic = brief._shell_scan_tokens(r'printf "%s" "$((items/total))"')

    assert brief.COMMAND_SUB_START not in literal
    assert brief.COMMAND_SUB_START not in escaped
    assert brief.COMMAND_SUB_START not in arithmetic


def test_dynamic_cd_substitution_does_not_change_outer_working_directory():
    assert brief._spec_paths(
        "cd \"$(printf apps/web)\" && cat tests/after.test.ts"
    ) == ["apps/web", "tests/after.test.ts"]
