"""Unit tests verifying tusk upgrade propagates conventions-preflight.sh.

Tests that copy_hooks() copies conventions-preflight.sh and that
merge_hook_registrations() adds the Edit|Write PreToolUse entry from source
settings.json into the target — covering the tusk upgrade code path.
"""

import importlib.util
import json
import os
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPGRADE_SCRIPT = os.path.join(REPO_ROOT, "bin", "tusk-upgrade.py")
HOOK_NAME = "conventions-preflight.sh"
REAL_HOOKS_SRC = os.path.join(REPO_ROOT, ".claude", "hooks")
REAL_SETTINGS_SRC = os.path.join(REPO_ROOT, ".claude", "settings.json")


def _load_module():
    spec = importlib.util.spec_from_file_location("tusk_upgrade", UPGRADE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_settings(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def _read_settings(path):
    return json.loads(path.read_text())


def _hook_entry_count(settings):
    """Count how many hooks in settings register conventions-preflight.sh."""
    count = 0
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for h in group.get("hooks", []):
                if HOOK_NAME in h.get("command", ""):
                    count += 1
    return count


class TestUpgradeConventionsPreflight:
    def test_copy_hooks_copies_conventions_preflight(self, tmp_path):
        """copy_hooks() copies conventions-preflight.sh from source to target .claude/hooks/."""
        mod = _load_module()
        src = tmp_path / "src"
        tgt = tmp_path / "tgt"
        src_hooks = src / ".claude" / "hooks"
        src_hooks.mkdir(parents=True)
        (src_hooks / HOOK_NAME).write_text("#!/bin/bash\nexit 0\n")

        tgt_claude = tgt / ".claude"
        tgt_claude.mkdir(parents=True)

        mod.copy_hooks(str(src), str(tgt))

        dest = tgt / ".claude" / "hooks" / HOOK_NAME
        assert dest.exists(), f"{HOOK_NAME} was not copied to target"
        assert os.access(str(dest), os.X_OK), f"{HOOK_NAME} is not executable after copy"

    def test_merge_adds_conventions_preflight_settings_entry(self, tmp_path):
        """merge_hook_registrations() adds the Edit|Write PreToolUse entry from source."""
        mod = _load_module()
        src_claude = tmp_path / "src" / ".claude"
        tgt_claude = tmp_path / "tgt" / ".claude"
        src_claude.mkdir(parents=True)
        tgt_claude.mkdir(parents=True)

        # Source has the conventions-preflight hook (as in the real tusk settings.json)
        _write_settings(src_claude / "settings.json", {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": ".claude/hooks/conventions-preflight.sh",
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            }
        })
        # Target has no settings yet
        mod.merge_hook_registrations(str(tmp_path / "src"), str(tmp_path / "tgt"))

        settings = _read_settings(tgt_claude / "settings.json")
        count = _hook_entry_count(settings)
        assert count == 1, f"Expected 1 conventions-preflight entry, found {count}"

        preflight_groups = [
            g
            for g in settings.get("hooks", {}).get("PreToolUse", [])
            if any(HOOK_NAME in h.get("command", "") for h in g.get("hooks", []))
        ]
        assert len(preflight_groups) == 1
        assert preflight_groups[0]["matcher"] == "Edit|Write"

    def test_upgrade_propagates_hook_file_and_settings_entry(self, tmp_path):
        """Full upgrade path: copy_hooks + merge_hook_registrations both propagate conventions-preflight."""
        mod = _load_module()
        src = tmp_path / "src"
        tgt = tmp_path / "tgt"

        src_hooks_dir = src / ".claude" / "hooks"
        src_hooks_dir.mkdir(parents=True)
        (tgt / ".claude").mkdir(parents=True)

        # Populate src with the real conventions-preflight.sh
        real_hook = os.path.join(REAL_HOOKS_SRC, HOOK_NAME)
        if os.path.isfile(real_hook):
            shutil.copy2(real_hook, str(src_hooks_dir / HOOK_NAME))
        else:
            (src_hooks_dir / HOOK_NAME).write_text("#!/bin/bash\nexit 0\n")

        # Source settings carries the Edit|Write PreToolUse entry
        _write_settings(src / ".claude" / "settings.json", {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": ".claude/hooks/conventions-preflight.sh",
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            }
        })

        mod.copy_hooks(str(src), str(tgt))
        mod.merge_hook_registrations(str(src), str(tgt))

        # Hook file is present and executable
        dest_hook = tgt / ".claude" / "hooks" / HOOK_NAME
        assert dest_hook.exists(), f"{HOOK_NAME} not copied by upgrade"
        assert os.access(str(dest_hook), os.X_OK)

        # Settings entry is present exactly once
        settings = _read_settings(tgt / ".claude" / "settings.json")
        count = _hook_entry_count(settings)
        assert count == 1, f"Expected 1 hook entry after upgrade, found {count}"

    def test_upgrade_idempotent_no_duplicate_entry(self, tmp_path):
        """Running copy_hooks + merge_hook_registrations twice does not duplicate the entry."""
        mod = _load_module()
        src = tmp_path / "src"
        tgt = tmp_path / "tgt"

        src_hooks_dir = src / ".claude" / "hooks"
        src_hooks_dir.mkdir(parents=True)
        (src_hooks_dir / HOOK_NAME).write_text("#!/bin/bash\nexit 0\n")
        (tgt / ".claude").mkdir(parents=True)

        src_settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": ".claude/hooks/conventions-preflight.sh",
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            }
        }
        _write_settings(src / ".claude" / "settings.json", src_settings)

        # Run twice
        mod.copy_hooks(str(src), str(tgt))
        mod.merge_hook_registrations(str(src), str(tgt))
        mod.copy_hooks(str(src), str(tgt))
        mod.merge_hook_registrations(str(src), str(tgt))

        settings = _read_settings(tgt / ".claude" / "settings.json")
        count = _hook_entry_count(settings)
        assert count == 1, (
            f"Expected 1 conventions-preflight entry after two upgrades, found {count}"
        )
