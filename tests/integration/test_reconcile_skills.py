"""Integration tests for `tusk reconcile-skills` (TASK-256).

Skills declaring `applies_to_project_types` should install only when the
target project's `tusk/config.json:project_type` matches one of the listed
types. install.sh and `tusk upgrade` apply that filter at install time, but
neither fires when project_type *changes* on an already-installed project.
`tusk reconcile-skills` is the explicit reconciliation surface — these tests
exercise both the no-change (idempotent) path and the type-change path
end-to-end against the live `bin/tusk` dispatcher.

Each test stands up a tmp git repo with a synthetic `skills/` tree (one
universal control skill plus two gated skills targeting different
project_types) and a `tusk/config.json` derived from `config.default.json`.
The reconcile subcommand is invoked via `--source-dir` to keep the tests
hermetic (no GitHub round-trip).
"""

import json
import os
import subprocess

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TESTS_DIR))
TUSK_BIN = os.path.join(REPO_ROOT, "bin", "tusk")


def _write_skill(skills_dir, name, *, gates=None):
    """Create skills_dir/<name>/SKILL.md with optional applies_to_project_types."""
    skill_dir = os.path.join(skills_dir, name)
    os.makedirs(skill_dir, exist_ok=True)
    body = ["---", f"name: {name}", "description: synthetic test skill", "allowed-tools: Bash"]
    if gates is not None:
        body.append(f"applies_to_project_types: [{', '.join(gates)}]")
    body.extend(["---", "", f"# {name}", "", "Test body.", ""])
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(body))


def _write_config(repo, project_type):
    """Write tusk/config.json derived from config.default.json with project_type stamped."""
    tusk_dir = os.path.join(repo, "tusk")
    os.makedirs(tusk_dir, exist_ok=True)
    with open(os.path.join(REPO_ROOT, "config.default.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["project_type"] = project_type
    with open(os.path.join(tusk_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)


@pytest.fixture()
def fake_repo(tmp_path):
    """Initialise a tmp git repo with three synthetic skills under skills/.

    - `_test_uni`     — universal (no gates)
    - `_test_ios`     — gated to ios_app
    - `_test_python`  — gated to python_service

    Returns the tmp_path; caller writes tusk/config.json with the desired
    project_type before running `tusk reconcile-skills`.
    """
    subprocess.run(
        ["git", "init", str(tmp_path)],
        capture_output=True, check=True, encoding="utf-8",
    )
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(str(skills), "_test_uni")
    _write_skill(str(skills), "_test_ios", gates=["ios_app"])
    _write_skill(str(skills), "_test_python", gates=["python_service"])
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _run_reconcile(repo):
    """Invoke `tusk reconcile-skills --json --source-dir <repo>/skills` from repo."""
    return subprocess.run(
        [TUSK_BIN, "reconcile-skills", "--json",
         "--source-dir", str(repo / "skills")],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_idempotent_no_change(fake_repo):
    """Second reconcile run on an unchanged project_type must be a no-op."""
    _write_config(str(fake_repo), "ios_app")

    r1 = _run_reconcile(fake_repo)
    assert r1.returncode == 0, f"first run failed: stderr={r1.stderr!r}"
    out1 = json.loads(r1.stdout)
    assert "_test_ios" in out1["installed"], (
        f"ios_app skill should install on project_type=ios_app; got {out1!r}"
    )
    assert "_test_python" not in out1["installed"]
    assert out1["removed"] == []
    skills_dir = fake_repo / ".claude" / "skills"
    assert (skills_dir / "_test_ios" / "SKILL.md").is_file()
    assert not (skills_dir / "_test_python").exists()

    r2 = _run_reconcile(fake_repo)
    assert r2.returncode == 0, f"second run failed: stderr={r2.stderr!r}"
    out2 = json.loads(r2.stdout)
    assert out2["installed"] == [], (
        f"second run must be idempotent; got installed={out2['installed']!r}"
    )
    assert out2["removed"] == [], (
        f"second run must be idempotent; got removed={out2['removed']!r}"
    )
    assert (skills_dir / "_test_ios" / "SKILL.md").is_file()


def test_idempotent_universal_never_managed(fake_repo):
    """Universal skills (no applies_to_project_types) must never appear in installed/removed."""
    _write_config(str(fake_repo), "ios_app")
    r = _run_reconcile(fake_repo)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "_test_uni" not in out["installed"]
    assert "_test_uni" not in out["removed"]
    assert "_test_uni" in out["skipped_universal"]


def test_project_type_change_swaps_gated_skills(fake_repo):
    """Changing project_type installs newly-matching skills and removes now-unmatched ones."""
    _write_config(str(fake_repo), "ios_app")
    _run_reconcile(fake_repo)

    skills_dir = fake_repo / ".claude" / "skills"
    assert (skills_dir / "_test_ios" / "SKILL.md").is_file(), "setup precondition"
    assert not (skills_dir / "_test_python").exists(), "setup precondition"

    _write_config(str(fake_repo), "python_service")
    r = _run_reconcile(fake_repo)
    assert r.returncode == 0, f"reconcile failed: stderr={r.stderr!r}"
    out = json.loads(r.stdout)
    assert "_test_python" in out["installed"], (
        f"python_service skill should install on type change; got {out!r}"
    )
    assert "_test_ios" in out["removed"], (
        f"ios_app skill should be removed on type change; got {out!r}"
    )

    assert (skills_dir / "_test_python" / "SKILL.md").is_file()
    assert not (skills_dir / "_test_ios").exists()


def test_project_type_change_to_null_removes_all_gated(fake_repo):
    """Clearing project_type (back to null) removes every gated skill."""
    _write_config(str(fake_repo), "ios_app")
    _run_reconcile(fake_repo)

    skills_dir = fake_repo / ".claude" / "skills"
    assert (skills_dir / "_test_ios").exists(), "setup precondition"

    _write_config(str(fake_repo), None)
    r = _run_reconcile(fake_repo)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "_test_ios" in out["removed"]
    assert out["installed"] == []
    assert not (skills_dir / "_test_ios").exists()
    assert not (skills_dir / "_test_python").exists()
