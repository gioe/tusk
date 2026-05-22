"""Integration test for tusk dupes check near-match surfacing (issue #772).

When no above-threshold duplicate exists, `tusk dupes check` now also prints
the top-3 nearest below-threshold open tasks (bounded by a similarity floor)
so the operator can eyeball structural overlap that pure Jaccard /
character-similarity missed — e.g. two summaries naming the same
camelCase entity but using different verbs.
"""

import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _run(cmd, env, check=True):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if check and result.returncode not in (0, 1):
        raise AssertionError(
            f"Command failed: {cmd}\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}\n"
            f"exit={result.returncode}"
        )
    return result


def _env(db_path):
    env = os.environ.copy()
    env["TUSK_DB"] = str(db_path)
    return env


def _insert(env, summary, description="seed", **extra):
    args = [
        TUSK_BIN, "task-insert", summary, description,
        "--priority", "Medium",
        "--task-type", "feature",
        "--complexity", "S",
        "--criteria", "seed criterion",
    ]
    for k, v in extra.items():
        args.extend([f"--{k.replace('_', '-')}", v])
    result = _run(args, env)
    return json.loads(result.stdout)["task_id"]


def test_near_matches_surfaced_when_no_above_threshold_match(db_path):
    """The MonthCalendarView repro from issue #772.

    Existing task: 'Add unit tests for MonthCalendarView range tap, leadingNils, and chevron disabling'
    Proposed:     'Add unit tests for MonthCalendarView date-math helpers'

    Token-set Jaccard ≈ 0.38, well below similar_threshold (0.6) and
    check_threshold (0.82) — no above-threshold match. Pre-#772 the
    operator got 'No duplicates found' and no other signal. Now they
    also see the existing task as a near-match.
    """
    env = _env(db_path)
    existing_id = _insert(
        env,
        "Add unit tests for MonthCalendarView range tap, leadingNils, and chevron disabling",
    )

    result = _run(
        [
            TUSK_BIN, "dupes", "check",
            "Add unit tests for MonthCalendarView date-math helpers",
            "--json",
        ],
        env,
    )

    payload = json.loads(result.stdout)
    assert payload["duplicates"] == [], "expected no above-threshold duplicates"
    near = payload["near_matches"]
    assert any(m["id"] == existing_id for m in near), (
        f"expected existing task #{existing_id} to appear in near_matches; "
        f"got {near}"
    )
    first = near[0]
    assert first["match_type"] == "near_match"
    assert 0 < first["similarity"] < 1.0


def test_near_matches_capped_at_three(db_path):
    """Near-matches list is bounded at 3 even with many candidates above the floor.

    Six summaries each shares the unique camelCase token MonthCalendarView with
    the probe but vary enough in other words to clear the built-in dupes guard
    on `tusk task-insert`. The probe is structurally close to all six (Jaccard
    drops the high-frequency stopwords but keeps the unique token), so each
    candidate sits in the near-match band rather than above check_threshold.
    """
    env = _env(db_path)
    candidates = [
        "Refactor MonthCalendarView grid layout for landscape orientation",
        "Audit MonthCalendarView accessibility labels for VoiceOver",
        "Replace MonthCalendarView legacy date provider with new injector",
        "Profile MonthCalendarView scroll performance on iPhone SE",
        "Add MonthCalendarView snapshot regression suite for dark mode",
        "Tighten MonthCalendarView holiday badge contrast ratios",
    ]
    for summary in candidates:
        _insert(env, summary)

    result = _run(
        [TUSK_BIN, "dupes", "check", "Add MonthCalendarView date-math helpers", "--json"],
        env,
    )

    payload = json.loads(result.stdout)
    assert payload["duplicates"] == [], (
        f"none of the candidates should land above threshold; got {payload['duplicates']}"
    )
    assert len(payload["near_matches"]) <= 3, (
        f"expected <=3 near_matches; got {len(payload['near_matches'])}: {payload['near_matches']}"
    )
    sims = [m["similarity"] for m in payload["near_matches"]]
    assert sims == sorted(sims, reverse=True), "near_matches must be sorted desc"


def test_near_matches_respect_similarity_floor(db_path):
    """The similarity floor suppresses tasks whose actual computed similarity
    against the probe is below 0.2. Per Convention 38, the test computes each
    candidate's similarity from the dupes scorer and asserts the (candidate
    in near_matches) ↔ (similarity ≥ floor) equivalence holds for every pair —
    rather than assuming any specific vocabulary pair lands above or below.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tusk_dupes", os.path.join(REPO_ROOT, "bin", "tusk-dupes.py")
    )
    tusk_dupes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tusk_dupes)

    probe = "Add MonthCalendarView date-math helpers"

    candidates = [
        "Refactor billing-pipeline failover retry counter",
        "Document iOS keychain entitlements for SSO",
        "Optimize Redis cache TTL for session lookups",
    ]
    env = _env(db_path)
    inserted_summaries = []
    for c in candidates:
        _insert(env, c)
        inserted_summaries.append(c)

    result = _run(
        [TUSK_BIN, "dupes", "check", probe, "--json"],
        env,
    )
    payload = json.loads(result.stdout)
    assert payload["duplicates"] == []

    surfaced_summaries = {m["summary"] for m in payload["near_matches"]}
    for summary in inserted_summaries:
        score = tusk_dupes.similarity(probe, summary)
        if score >= 0.2:
            assert summary in surfaced_summaries, (
                f"summary {summary!r} (similarity {score:.3f}) ≥ 0.2 should be surfaced"
            )
        else:
            assert summary not in surfaced_summaries, (
                f"summary {summary!r} (similarity {score:.3f}) < 0.2 must be suppressed"
            )


def test_above_threshold_match_suppresses_near_matches_in_text_output(db_path):
    """When a real duplicate is found, the text output does not also dump near-matches.

    JSON output always includes both; text output only shows near-matches when
    no above-threshold duplicate was found (otherwise it's just noise).
    """
    env = _env(db_path)
    existing_id = _insert(env, "Add login endpoint with JWT auth")
    _insert(env, "Refactor billing pipeline retries")

    # Identical-ish summary → above-threshold match
    result = _run(
        [TUSK_BIN, "dupes", "check", "Add login endpoint with JWT auth"],
        env,
    )
    assert result.returncode == 1, "above-threshold match should exit 1"
    assert "Top" not in result.stdout or "nearest below threshold" not in result.stdout, (
        f"unexpected near-match section in text output when duplicates found:\n{result.stdout}"
    )
    assert "Duplicates found" in result.stdout
    assert str(existing_id) in result.stdout


def test_exit_code_unchanged_for_no_match(db_path):
    """Exit code 0 still means 'no above-threshold duplicate' even when near_matches is non-empty."""
    env = _env(db_path)
    _insert(env, "Add unit tests for MonthCalendarView range tap")

    result = _run(
        [TUSK_BIN, "dupes", "check", "Add MonthCalendarView date-math helpers", "--json"],
        env,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["duplicates"] == []
    # Whether near_matches is populated or empty, exit code is 0 either way.
    assert "near_matches" in payload
