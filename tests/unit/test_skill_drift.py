"""Unit tests for the skill/CLI subcommand drift detector (issue #1035).

Covers the two invariants the /address-issue task pinned:
  - In-sync source repo -> zero drift (no false positives), including the
    slash-command and prose-mention guards that make that hold.
  - Stale CLI (skills reference a subcommand the dispatcher lacks) -> drift,
    with an explicit `tusk upgrade` recommendation.
"""

import importlib.util
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "tusk_skill_drift", os.path.join(REPO_ROOT, "bin", "tusk-skill-drift.py")
)
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)


# A minimal dispatcher whose column-0 `case "${1:-}" in` block enumerates the
# "installed" subcommands. The indented helper-function `case` block must NOT
# leak its alternation arms into the parsed set.
_FAKE_TUSK = '''#!/usr/bin/env bash
is_readonly_subcmd() {
  case "${1:-}" in
    ""|path|config|validate)
      return 0 ;;
    *) return 1 ;;
  esac
}

case "${1:-}" in
  init)   shift; cmd_init "$@" ;;
  commit) shift; foo "$@" ;;
  task-start) shift; foo "$@" ;;
  review) shift; foo "$@" ;;
  "")
          echo usage ;;
  *)      cmd_query "$@" ;;
esac
'''


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _build_repo(tmp_path, skill_md, tusk_src=_FAKE_TUSK):
    repo = os.path.join(str(tmp_path), "repo")
    _write(os.path.join(repo, "bin", "tusk"), tusk_src)
    _write(os.path.join(repo, ".claude", "skills", "demo", "SKILL.md"), skill_md)
    return repo


class TestParseInstalledSubcommands:
    def test_extracts_only_dispatcher_arms(self, tmp_path):
        p = os.path.join(str(tmp_path), "tusk")
        _write(p, _FAKE_TUSK)
        cmds = drift.parse_installed_subcommands(p)
        assert cmds == {"init", "commit", "task-start", "review"}

    def test_helper_case_arms_do_not_leak(self, tmp_path):
        # `path`/`config`/`validate` live only in the indented helper case block;
        # they must not be treated as installed subcommands.
        p = os.path.join(str(tmp_path), "tusk")
        _write(p, _FAKE_TUSK)
        cmds = drift.parse_installed_subcommands(p)
        assert "path" not in cmds and "config" not in cmds and "validate" not in cmds

    def test_missing_file_returns_empty(self, tmp_path):
        assert drift.parse_installed_subcommands(os.path.join(str(tmp_path), "nope")) == set()


class TestTokenExtraction:
    def test_first_token_is_the_subcommand(self):
        # `tusk review begin` dispatches to `review`; `begin` is an arg.
        assert list(drift._iter_subcommand_tokens("tusk review begin")) == ["review"]
        assert list(drift._iter_subcommand_tokens("tusk scope list 5")) == ["scope"]

    def test_bin_path_form_is_a_command(self):
        assert list(drift._iter_subcommand_tokens("bin/tusk scope add")) == ["scope"]
        assert list(drift._iter_subcommand_tokens("$workspace/bin/tusk merge x")) == ["merge"]

    def test_command_substitution_and_pipe_are_commands(self):
        assert list(drift._iter_subcommand_tokens("$(tusk path)")) == ["path"]
        assert list(drift._iter_subcommand_tokens("foo | tusk jots")) == ["jots"]

    def test_slash_command_is_not_a_cli_reference(self):
        # `/tusk done` is a skill invocation, not `tusk done` on the CLI.
        assert list(drift._iter_subcommand_tokens("/tusk done")) == []
        assert list(drift._iter_subcommand_tokens("run `/tusk blocked` now".replace("`", ""))) == []

    def test_prose_word_before_tusk_is_not_a_command(self):
        assert list(drift._iter_subcommand_tokens("the tusk database")) == []
        assert list(drift._iter_subcommand_tokens("Originating tusk task: TASK-5")) == []
        assert list(drift._iter_subcommand_tokens("from a tusk client repo")) == []

    def test_hyphenated_subcommand(self):
        assert list(drift._iter_subcommand_tokens("tusk task-worktree create")) == ["task-worktree"]

    def test_tusk_hyphen_script_is_not_matched(self):
        # `tusk-lint.py` is a script name, not a `tusk <subcommand>` invocation.
        assert list(drift._iter_subcommand_tokens("see bin/tusk-lint.py")) == []


class TestCodeContextScan:
    def test_inline_code_span_scanned(self):
        text = "Run `tusk scope add 5 path` before committing."
        chunks = list(drift._scan_code_context(text))
        assert any("tusk scope add" in c for c in chunks)

    def test_prose_outside_code_is_not_scanned(self):
        text = "The tusk database lives under tusk/."
        # No backticks/fences: nothing yielded, so no tokens.
        assert list(drift._scan_code_context(text)) == []

    def test_fenced_block_scanned(self):
        text = "```bash\ntusk task-start 5\n```\n"
        chunks = list(drift._scan_code_context(text))
        assert any("tusk task-start" in c for c in chunks)


class TestComputeDrift:
    def test_in_sync_no_drift(self, tmp_path):
        skill = "Run `tusk init`, then `tusk commit 5 msg f`, then `tusk review begin 5`.\n"
        repo = _build_repo(tmp_path, skill)
        d, refs, installed, files = drift.compute_drift(
            repo, tusk_path=os.path.join(repo, "bin", "tusk")
        )
        assert d == {}
        assert "review" in refs and "commit" in refs
        assert len(files) == 1

    def test_stale_cli_reports_drift(self, tmp_path):
        skill = "Use `tusk scope add 5 p` and `tusk task-worktree create 5 s`. Also `tusk init`.\n"
        repo = _build_repo(tmp_path, skill)
        d, refs, installed, files = drift.compute_drift(
            repo, tusk_path=os.path.join(repo, "bin", "tusk")
        )
        assert set(d) == {"scope", "task-worktree"}
        assert d["scope"] == [os.path.join(repo, ".claude", "skills", "demo", "SKILL.md")]

    def test_slash_command_and_prose_never_drift(self, tmp_path):
        skill = (
            "Suggest `/tusk blocked` or `/tusk wip`. The tusk database is shared.\n"
            "Originating tusk task: TASK-5.\n"
        )
        repo = _build_repo(tmp_path, skill)
        d, _, _, _ = drift.compute_drift(repo, tusk_path=os.path.join(repo, "bin", "tusk"))
        assert d == {}

    def test_no_skills_is_not_drift(self, tmp_path):
        repo = os.path.join(str(tmp_path), "repo")
        _write(os.path.join(repo, "bin", "tusk"), _FAKE_TUSK)
        d, refs, installed, files = drift.compute_drift(
            repo, tusk_path=os.path.join(repo, "bin", "tusk")
        )
        assert d == {} and files == []


class TestIsReferenced:
    def test_referenced_subcommand(self, tmp_path):
        skill = "Use `tusk scope add 5 p`.\n"
        repo = _build_repo(tmp_path, skill)
        refs = drift.referenced_subcommands(drift.find_skill_files(repo))
        assert "scope" in refs

    def test_slash_command_not_referenced(self, tmp_path):
        skill = "Suggest `/tusk done` to the user.\n"
        repo = _build_repo(tmp_path, skill)
        refs = drift.referenced_subcommands(drift.find_skill_files(repo))
        assert "done" not in refs


class TestRealRepoNoDrift:
    """The source repo ships skills and CLI in sync — the detector must report
    zero drift against the real tree (criterion: no false positives)."""

    def test_source_repo_has_no_drift(self):
        d, refs, installed, files = drift.compute_drift(REPO_ROOT)
        assert installed, "dispatcher parse must find subcommands"
        assert files, "real skills must be discovered"
        assert d == {}, f"unexpected drift in source repo: {sorted(d)}"
