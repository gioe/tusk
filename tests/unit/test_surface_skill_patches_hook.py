"""Unit tests for the surface-skill-patches.sh SessionStart hook.

The hook is bash glue around `tusk retro-patches --window-days N --unconfirmed`
plus a Python filter that drops rows whose target_file does not correspond
to a skill (or CLAUDE.md / AGENTS.md) loaded in the current session. The
filter is the only piece of logic worth covering — the bash front-end is a
thin wrapper. We exercise the hook end-to-end by:

1. `git init`-ing a tempdir so hook-common.sh can resolve REPO_ROOT.
2. Seeding `.claude/skills/<name>/SKILL.md` and CLAUDE.md/AGENTS.md fixtures
   to control which target_files are "loaded".
3. Writing a stub `bin/tusk` that emits a fixed retro-patches JSON payload,
   so the hook never touches a real tusk DB.
4. Running the actual hook script from the source tree, with cwd pinned to
   the tempdir.

This covers the filter behavior without coupling to retro_findings schema
internals or requiring a populated tusk install in the test environment.
"""

import json
import os
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO_ROOT, ".claude", "hooks", "surface-skill-patches.sh")


def _row(finding_id: int, task_id: int, target_file: str, age_days: int = 5) -> dict:
    return {
        "finding_id": finding_id,
        "skill_run_id": finding_id,
        "task_id": task_id,
        "action_taken": f"skill-patch:{target_file}",
        "target_file": target_file,
        "created_at": "2026-05-01 00:00:00",
        "age_days": age_days,
    }


def _setup_repo(
    tmp_path,
    *,
    rows: list,
    loaded_skills: list = (),
    include_claude_md: bool = True,
    include_agents_md: bool = False,
):
    """Init a fake repo at tmp_path. Returns (cwd, env) for subprocess.run."""
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])

    if include_claude_md:
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n")
    if include_agents_md:
        (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n")

    skills_root = tmp_path / ".claude" / "skills"
    skills_root.mkdir(parents=True)
    for name in loaded_skills:
        skill_dir = skills_root / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"# {name}\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = json.dumps(rows)
    fake_tusk = bin_dir / "tusk"
    # The hook invokes "$TUSK" retro-patches --window-days N --unconfirmed.
    # The stub matches on $1 and prints payload via heredoc — no shell
    # interpretation of the JSON content (single-quoted EOF).
    fake_tusk.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "retro-patches" ]; then\n'
        "  cat <<'EOF'\n"
        f"{payload}\n"
        "EOF\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake_tusk.chmod(0o755)

    env = {
        **os.environ,
        # Drop any inherited TUSK_DB / TUSK_PROJECT so the stub takes effect.
        "TUSK_DB": "",
        "TUSK_PROJECT": "",
    }
    env.pop("TUSK_DB", None)
    env.pop("TUSK_PROJECT", None)
    return str(tmp_path), env


def _run_hook(cwd: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", HOOK],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


class TestSurfaceSkillPatchesHook:
    def test_empty_list_silent_exit(self, tmp_path):
        cwd, env = _setup_repo(tmp_path, rows=[], loaded_skills=["tusk"])
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_loaded_skill_match_emits_paragraph(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "skills/tusk/SKILL.md", age_days=3)],
            loaded_skills=["tusk"],
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "skills/tusk/SKILL.md" in r.stdout
        assert "TASK-100" in r.stdout
        assert "3d ago" in r.stdout
        assert "skill-patch-confirmed" in r.stdout
        assert "/retro" in r.stdout

    def test_unloaded_skill_filtered_out(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[
                _row(1, 100, "skills/tusk/SKILL.md"),
                _row(2, 101, "skills/missing/SKILL.md"),
            ],
            loaded_skills=["tusk"],
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "skills/tusk/SKILL.md" in r.stdout
        assert "skills/missing/SKILL.md" not in r.stdout

    def test_no_matches_silent_exit(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "skills/missing/SKILL.md")],
            loaded_skills=["tusk"],
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_claude_md_target_matches(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "CLAUDE.md")],
            loaded_skills=[],
            include_claude_md=True,
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "CLAUDE.md" in r.stdout
        assert "TASK-100" in r.stdout

    def test_agents_md_target_matches_when_present(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "AGENTS.md")],
            loaded_skills=[],
            include_claude_md=False,
            include_agents_md=True,
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "AGENTS.md" in r.stdout

    def test_agents_md_target_dropped_when_absent(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "AGENTS.md")],
            loaded_skills=["tusk"],
            include_claude_md=True,
            include_agents_md=False,
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "AGENTS.md" not in r.stdout

    def test_compound_target_keeps_only_loaded_components(self, tmp_path):
        # target_file may be comma-separated for compound skill-patches that
        # touched several files in one retro pass. Only the components whose
        # skills are actually present should appear in the output.
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "skills/tusk/SKILL.md,skills/missing/SKILL.md")],
            loaded_skills=["tusk"],
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "skills/tusk/SKILL.md" in r.stdout
        assert "skills/missing/SKILL.md" not in r.stdout

    def test_compound_target_with_no_loaded_components_dropped(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "skills/foo/SKILL.md,skills/bar/SKILL.md")],
            loaded_skills=["tusk"],
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_disable_env_var_silences_hook(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[_row(1, 100, "skills/tusk/SKILL.md")],
            loaded_skills=["tusk"],
        )
        env["TUSK_NO_SKILL_PATCH_NOTICE"] = "1"
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert r.stdout == ""

    def test_multiple_matches_joined_in_paragraph(self, tmp_path):
        cwd, env = _setup_repo(
            tmp_path,
            rows=[
                _row(1, 100, "skills/tusk/SKILL.md", age_days=2),
                _row(2, 101, "CLAUDE.md", age_days=7),
            ],
            loaded_skills=["tusk"],
        )
        r = _run_hook(cwd, env)
        assert r.returncode == 0
        assert "skills/tusk/SKILL.md" in r.stdout
        assert "CLAUDE.md" in r.stdout
        assert "TASK-100" in r.stdout
        assert "TASK-101" in r.stdout
        # Single-line paragraph — entries separated by '; '
        assert "; " in r.stdout
