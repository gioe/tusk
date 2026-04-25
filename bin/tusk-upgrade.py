#!/usr/bin/env python3
"""Upgrade tusk from GitHub.

Called by the tusk wrapper:
    tusk upgrade [--no-commit] [--force]
    → tusk-upgrade.py <REPO_ROOT> <SCRIPT_DIR> [--no-commit] [--force]

Arguments:
    sys.argv[1] — absolute path to the repo root
    sys.argv[2] — absolute path to the script dir (.claude/bin or bin/)
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _ssl_context() -> ssl.SSLContext:
    """Return an SSL context with system/certifi certs, falling back to default."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(capath="/etc/ssl/certs")
    except (FileNotFoundError, ssl.SSLError):
        pass
    return ctx

GITHUB_REPO = "gioe/tusk"
API_TIMEOUT = 15   # seconds for GitHub API calls
DL_TIMEOUT = 60    # seconds for tarball download

# Supported install modes and their canonical directory layouts. The marker
# file <script_dir>/install-mode is stamped by install.sh; absent → claude
# (legacy installs predate Codex support).
INSTALL_MODES = {
    "claude": {
        "bin_prefix": ".claude/bin/",
        "manifest_rel": ".claude/tusk-manifest.json",
    },
    "codex": {
        "bin_prefix": "tusk/bin/",
        "manifest_rel": "tusk/tusk-manifest.json",
    },
}


def detect_install_mode(script_dir: str) -> str:
    """Return install mode from the <script_dir>/install-mode marker.

    Absent or malformed → 'claude' (legacy pre-Codex installs and dev envs).
    """
    marker = os.path.join(script_dir, "install-mode")
    if os.path.isfile(marker):
        try:
            value = Path(marker).read_text().strip()
        except OSError:
            return "claude"
        if value in INSTALL_MODES:
            return value
    return "claude"


def translate_manifest_for_mode(files, mode: str) -> list:
    """Rewrite tarball MANIFEST entries (mode-shaped) for the local install mode.

    The tarball MANIFEST may contain both .claude/* (claude-only) and .codex/* (codex-only)
    paths since a single tarball ships both. Translation:
    - Claude mode: keep .claude/* and pass-through; drop .codex/* (no Claude equivalents).
    - Codex mode: rewrite .claude/bin/ → tusk/bin/, drop .claude/skills/ and .claude/hooks/
      (no Codex equivalents), keep .codex/* unchanged.
    """
    if mode == "claude":
        return [f for f in files if not f.startswith(".codex/")]
    bin_prefix = INSTALL_MODES[mode]["bin_prefix"]
    out = []
    for f in files:
        if f.startswith(".claude/bin/"):
            out.append(bin_prefix + f[len(".claude/bin/"):])
        elif f.startswith(".claude/skills/") or f.startswith(".claude/hooks/"):
            continue
        else:
            out.append(f)
    return out

# Verbosity flag. Defaults to True so tests and direct imports keep the legacy
# loud output; main() sets it to args.verbose so routine CLI upgrades are quiet.
_verbose = True


def _vprint(*args, **kwargs) -> None:
    if _verbose:
        print(*args, **kwargs)


def is_source_repo(repo_root: str) -> bool:
    """Return True when repo_root looks like the tusk source repo.

    Three markers (all must hold):
    - skills/ is a real directory (not a symlink) with at least one subdir
      containing a SKILL.md file
    - install.sh is a regular file at repo root
    - .claude/skills/ exists and every entry in it is a symlink

    In installed projects, .claude/skills/ contains real directories (copied
    from the tarball) and there is no skills/ sibling — so the combined test
    distinguishes source from target without relying on a path name.
    """
    skills_dir = os.path.join(repo_root, "skills")
    if os.path.islink(skills_dir) or not os.path.isdir(skills_dir):
        return False
    try:
        skills_entries = os.listdir(skills_dir)
    except OSError:
        return False
    has_skill_md = any(
        os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))
        for name in skills_entries
        if os.path.isdir(os.path.join(skills_dir, name))
    )
    if not has_skill_md:
        return False

    if not os.path.isfile(os.path.join(repo_root, "install.sh")):
        return False

    claude_skills = os.path.join(repo_root, ".claude", "skills")
    if not os.path.isdir(claude_skills):
        return False
    try:
        claude_entries = [e for e in os.listdir(claude_skills) if not e.startswith(".")]
    except OSError:
        return False
    if not claude_entries:
        return False
    return all(
        os.path.islink(os.path.join(claude_skills, e))
        for e in claude_entries
    )


def _should_rexec(src: str, script_dir: str) -> bool:
    """Return True when the upgrader in the tarball differs from the installed one.

    Closes the bootstrap gap where 'tusk upgrade' runs the *installed* (older)
    tusk-upgrade.py to install a newer one — any UX change to the upgrader itself
    only takes effect on the next run unless we hand off to the freshly-extracted
    copy mid-upgrade.
    """
    old = os.path.join(script_dir, "tusk-upgrade.py")
    new = os.path.join(src, "bin", "tusk-upgrade.py")
    if not os.path.isfile(old) or not os.path.isfile(new):
        return False
    return hashlib.md5(Path(old).read_bytes()).hexdigest() != hashlib.md5(Path(new).read_bytes()).hexdigest()


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def fetch_bytes(url: str, timeout: int = API_TIMEOUT) -> bytes:
    req = Request(url, headers={"User-Agent": "tusk-upgrade"})
    try:
        with urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except HTTPError as e:
        raise SystemExit(f"Error: HTTP {e.code} fetching {url}") from e
    except URLError as e:
        raise SystemExit(f"Error: Could not reach {url}: {e.reason}") from e


def get_latest_tag() -> str:
    data = fetch_bytes(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    )
    try:
        return json.loads(data)["tag_name"]
    except (KeyError, json.JSONDecodeError) as e:
        raise SystemExit(f"Error: Could not parse latest release from GitHub: {e}") from e


def get_remote_version(tag: str) -> int:
    raw = fetch_bytes(
        f"https://raw.githubusercontent.com/{GITHUB_REPO}/refs/tags/{tag}/VERSION"
    )
    try:
        return int(raw.strip())
    except ValueError as e:
        raise SystemExit(f"Error: Could not parse remote VERSION: {e}") from e


# ── Upgrade steps ─────────────────────────────────────────────────────────────

def remove_orphans(old_manifest_path: str, new_manifest_path: str, repo_root: str) -> int:
    with open(old_manifest_path) as f:
        old_files = set(json.load(f))
    with open(new_manifest_path) as f:
        new_files = set(json.load(f))
    orphans = old_files - new_files
    removed = 0
    for rel_path in sorted(orphans):
        full_path = os.path.join(repo_root, rel_path)
        if os.path.isfile(full_path):
            os.remove(full_path)
            removed += 1
            _vprint(f"  Removed orphan: {rel_path}")
            parent = os.path.dirname(full_path)
            try:
                os.rmdir(parent)
                _vprint(f"  Removed empty dir: {os.path.relpath(parent, repo_root)}")
            except OSError:
                _vprint(
                    f"  Kept non-empty dir (user files present): "
                    f"{os.path.relpath(parent, repo_root)}"
                )
    return removed


def copy_bin_files(src: str, script_dir: str) -> None:
    """Copy CLI and support files; use atomic rename for the running tusk binary."""
    tusk_tmp = os.path.join(script_dir, "tusk.tmp")
    shutil.copy2(os.path.join(src, "bin", "tusk"), tusk_tmp)
    os.chmod(tusk_tmp, 0o755)
    os.replace(tusk_tmp, os.path.join(script_dir, "tusk"))
    for pyfile in Path(os.path.join(src, "bin")).glob("tusk-*.py"):
        dest = os.path.join(script_dir, pyfile.name)
        if pyfile.name == "tusk-lint.py":
            src_hash = hashlib.md5(pyfile.read_bytes()).hexdigest()
            hash_sidecar = os.path.join(script_dir, "tusk-lint.py.hash")
            if os.path.isfile(dest) and os.path.isfile(hash_sidecar):
                # Only warn if the installed file has diverged from what tusk last wrote
                # (i.e., a true local modification), not on every routine upgrade.
                dest_hash = hashlib.md5(Path(dest).read_bytes()).hexdigest()
                baseline_hash = Path(hash_sidecar).read_text().strip()
                if dest_hash != baseline_hash:
                    print(f"  Warning: {dest} has local modifications and will be overwritten.")
                    print("  If you have project-specific lint rules in tusk-lint.py,")
                    print("  move them to tusk-lint-extra.py (never overwritten by upgrade).")
            # No sidecar: existing install without hash tracking — skip warning (graceful degradation).
        shutil.copy2(str(pyfile), script_dir)
        if pyfile.name == "tusk-lint.py":
            # Record what tusk just wrote so future upgrades can detect local modifications.
            Path(hash_sidecar).write_text(src_hash + "\n")
    # tusk_loader.py uses an underscore filename — copy explicitly (missed by glob above).
    shutil.copy2(os.path.join(src, "bin", "tusk_loader.py"), script_dir)
    shutil.copy2(
        os.path.join(src, "config.default.json"),
        os.path.join(script_dir, "config.default.json"),
    )
    shutil.copy2(
        os.path.join(src, "pricing.json"),
        os.path.join(script_dir, "pricing.json"),
    )
    _vprint("  Updated CLI and support files")


def copy_skills(src: str, repo_root: str) -> int:
    skills_src = os.path.join(src, "skills")
    if not os.path.isdir(skills_src):
        return 0
    count = 0
    for skill_name in os.listdir(skills_src):
        skill_dir = os.path.join(skills_src, skill_name)
        if not os.path.isdir(skill_dir):
            continue
        dest_dir = os.path.join(repo_root, ".claude", "skills", skill_name)
        os.makedirs(dest_dir, exist_ok=True)
        for fname in os.listdir(skill_dir):
            src_file = os.path.join(skill_dir, fname)
            if os.path.isfile(src_file):
                shutil.copy2(src_file, dest_dir)
        count += 1
        _vprint(f"  Updated skill: {skill_name}")
    return count


def copy_prompts(src: str, repo_root: str) -> int:
    prompts_src = os.path.join(src, "codex-prompts")
    if not os.path.isdir(prompts_src):
        return 0
    dest_dir = os.path.join(repo_root, ".codex", "prompts")
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    for fname in os.listdir(prompts_src):
        if not fname.endswith(".md"):
            continue
        src_file = os.path.join(prompts_src, fname)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, dest_dir)
            count += 1
            _vprint(f"  Updated codex prompt: {fname}")
    return count


def copy_scripts(src: str, repo_root: str) -> int:
    scripts_src = os.path.join(src, "scripts")
    if not os.path.isdir(scripts_src):
        return 0
    os.makedirs(os.path.join(repo_root, "scripts"), exist_ok=True)
    count = 0
    for script in Path(scripts_src).glob("*.py"):
        shutil.copy2(str(script), os.path.join(repo_root, "scripts"))
        count += 1
        _vprint(f"  Updated scripts/{script.name}")
    return count


def copy_hooks(src: str, repo_root: str) -> int:
    hooks_src = os.path.join(src, ".claude", "hooks")
    if not os.path.isdir(hooks_src):
        return 0
    hooks_dest = os.path.join(repo_root, ".claude", "hooks")
    os.makedirs(hooks_dest, exist_ok=True)
    count = 0
    for hookfile in os.listdir(hooks_src):
        src_hook = os.path.join(hooks_src, hookfile)
        if not os.path.isfile(src_hook):
            continue
        dest_hook = os.path.join(hooks_dest, hookfile)
        shutil.copy2(src_hook, dest_hook)
        os.chmod(dest_hook, 0o755)
        count += 1
        _vprint(f"  Updated hook: {hookfile}")
    return count


def override_setup_path(repo_root: str) -> None:
    """Write the target-project variant of setup-path.sh."""
    setup_path = os.path.join(repo_root, ".claude", "hooks", "setup-path.sh")
    with open(setup_path, "w") as f:
        f.write(
            "#!/bin/bash\n"
            "# Added by tusk install — puts .claude/bin on PATH for Claude Code sessions\n"
            'if [ -n "$CLAUDE_ENV_FILE" ]; then\n'
            '  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"\n'
            '  echo "export PATH=\\"$REPO_ROOT/.claude/bin:\\$PATH\\"" >> "$CLAUDE_ENV_FILE"\n'
            "fi\n"
            "exit 0\n"
        )
    os.chmod(setup_path, 0o755)


def merge_config_defaults(src: str, repo_root: str, script_dir: str) -> list[str]:
    """Backfill keys present in config.default.json but absent from config.json.

    Existing values in config.json are never overwritten — only missing keys are
    added with the default value from config.default.json. Returns the list of
    keys that were backfilled.
    """
    project_config = os.path.join(repo_root, "tusk", "config.json")
    if not os.path.isfile(project_config):
        return []  # No installed config — nothing to backfill

    # Prefer the default config from the freshly-extracted src directory so we
    # always merge against the latest defaults, not the previously installed ones.
    default_config = os.path.join(src, "config.default.json")
    if not os.path.isfile(default_config):
        # Fallback to the already-installed copy (e.g. local dev / test scenario)
        default_config = os.path.join(script_dir, "config.default.json")
    if not os.path.isfile(default_config):
        return []

    try:
        with open(default_config) as f:
            defaults = json.load(f)
        with open(project_config) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  Warning: could not parse config for backfill: {e}", flush=True)
        return []

    added = []
    for key, value in defaults.items():
        if key not in config:
            config[key] = value
            added.append(key)

    if added:
        with open(project_config, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        _vprint(f"  Backfilled config keys: {', '.join(added)}")
    return added


def _normalize_hook_cmd(cmd: str) -> str:
    """Normalize a hook command path for dedup comparison.

    Strips the $CLAUDE_PROJECT_DIR/ prefix (written by current install) and
    leading ./ (written by older installs) so that both path forms compare equal.
    Also handles the git-root-resolved wrapper form written by some older installs:
        bash -c 'R=$(git rev-parse ...); exec "$R/.claude/hooks/foo.sh"'
    extracts the .claude/hooks/<name> portion so it compares equal to the plain-path form.
    """
    prefix = "$CLAUDE_PROJECT_DIR/"
    if cmd.startswith(prefix):
        return cmd[len(prefix):]
    if cmd.startswith("./"):
        return cmd[2:]
    m = re.search(r'exec "\$R/(.claude/hooks/[^"\']+)"', cmd)
    if m:
        return m.group(1)
    return cmd


def _dedup_hook_groups(groups: list) -> list:
    """Remove duplicate hook groups, keeping the first occurrence of each normalized command."""
    seen: set = set()
    deduped = []
    for group in groups:
        commands = [
            _normalize_hook_cmd(h.get("command", ""))
            for h in group.get("hooks", [])
            if h.get("command")
        ]
        if any(cmd in seen for cmd in commands):
            continue
        deduped.append(group)
        seen.update(commands)
    return deduped


def merge_hook_registrations(src: str, repo_root: str) -> dict:
    """Merge source hook registrations and permissions into target settings.json.

    Returns a summary dict: {"registered": N, "dedup_removed": N, "permissions_added": N}.
    """
    source_settings_path = os.path.join(src, ".claude", "settings.json")
    target_settings_path = os.path.join(repo_root, ".claude", "settings.json")

    summary = {"registered": 0, "dedup_removed": 0, "permissions_added": 0}

    if not os.path.isfile(source_settings_path):
        return summary

    try:
        with open(source_settings_path) as f:
            source_settings = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Error: Could not parse source settings.json: {e}") from e

    source_hooks = source_settings.get("hooks", {})
    source_allow = source_settings.get("permissions", {}).get("allow", [])

    if os.path.exists(target_settings_path):
        try:
            with open(target_settings_path) as f:
                target_settings = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Error: Could not parse target settings.json: {e}") from e
    else:
        target_settings = {}

    target_hooks = target_settings.setdefault("hooks", {})

    # Dedup pass: remove duplicate hook groups already present in target settings
    for event_type in list(target_hooks.keys()):
        before = len(target_hooks[event_type])
        target_hooks[event_type] = _dedup_hook_groups(target_hooks[event_type])
        removed = before - len(target_hooks[event_type])
        if removed:
            summary["dedup_removed"] += removed
            _vprint(f"  Removed {removed} duplicate hook group(s) from {event_type}")

    for event_type, source_groups in source_hooks.items():
        target_groups = target_hooks.setdefault(event_type, [])
        existing_commands = set()
        for group in target_groups:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if cmd:
                    existing_commands.add(_normalize_hook_cmd(cmd))
        for group in source_groups:
            group_commands = [h.get("command", "") for h in group.get("hooks", [])]
            if not any(_normalize_hook_cmd(cmd) in existing_commands for cmd in group_commands if cmd):
                target_groups.append(group)
                for cmd in group_commands:
                    if cmd:
                        summary["registered"] += 1
                        _vprint(f"  Registered hook: {cmd}")
            else:
                for cmd in group_commands:
                    if cmd:
                        _vprint(f"  Hook already registered: {_normalize_hook_cmd(cmd)}")

    # Merge permissions.allow entries (same logic as install.sh step 4b)
    target_allow = target_settings.setdefault("permissions", {}).setdefault("allow", [])
    existing_allow = set(target_allow)
    for entry in source_allow:
        if entry not in existing_allow:
            target_allow.append(entry)
            existing_allow.add(entry)
            summary["permissions_added"] += 1
            _vprint(f"  Added permission: {entry}")
        else:
            _vprint(f"  Permission already present: {entry}")

    with open(target_settings_path, "w") as f:
        json.dump(target_settings, f, indent=2)
        f.write("\n")

    return summary


REQUIRED_REVIEW_COMMITS_PERMISSIONS = [
    "Bash(git diff:*)",
    "Bash(git remote:*)",
    "Bash(git symbolic-ref:*)",
    "Bash(git branch:*)",
    "Bash(tusk review:*)",
]


def check_review_commits_permissions(repo_root: str) -> list[str]:
    """Return any required permissions.allow entries missing from .claude/settings.json."""
    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    if not os.path.isfile(settings_path):
        return list(REQUIRED_REVIEW_COMMITS_PERMISSIONS)
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return list(REQUIRED_REVIEW_COMMITS_PERMISSIONS)
    existing = set(settings.get("permissions", {}).get("allow", []))
    return [e for e in REQUIRED_REVIEW_COMMITS_PERMISSIONS if e not in existing]


def ensure_review_commits_permissions(repo_root: str) -> list[str]:
    """Ensure REQUIRED_REVIEW_COMMITS_PERMISSIONS are present in .claude/settings.json.

    Creates the file (and any missing structure) if absent. Returns the list of
    entries that were added. If all required entries are already present, the
    file is left byte-identical (no-op write).
    """
    claude_dir = os.path.join(repo_root, ".claude")
    settings_path = os.path.join(claude_dir, "settings.json")

    if os.path.isfile(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}
    else:
        settings = {}

    permissions = settings.setdefault("permissions", {})
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow

    existing = set(allow)
    added: list[str] = []
    for entry in REQUIRED_REVIEW_COMMITS_PERMISSIONS:
        if entry not in existing:
            allow.append(entry)
            existing.add(entry)
            added.append(entry)

    if not added and os.path.isfile(settings_path):
        return []

    os.makedirs(claude_dir, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return added


def remove_deprecated_files(repo_root: str) -> int:
    removed = 0
    for rel in ["tusk/conventions.md", "tusk/dashboard.html", "tusk/tusk.db"]:
        full = os.path.join(repo_root, rel)
        if os.path.isfile(full):
            os.remove(full)
            removed += 1
            _vprint(f"  Removed deprecated file: {rel}")
    return removed


def update_gitignore(script_dir: str) -> None:
    kwargs = {"check": True}
    if not _verbose:
        kwargs["stdout"] = subprocess.DEVNULL
    subprocess.run([os.path.join(script_dir, "tusk"), "update-gitignore"], **kwargs)


def fix_trailing_newlines(script_dir: str, repo_root: str) -> int:
    candidates = [
        *glob.glob(os.path.join(script_dir, "*.json")),
        *glob.glob(os.path.join(script_dir, "*.py")),
        os.path.join(script_dir, "tusk"),
        *glob.glob(os.path.join(repo_root, ".claude", "skills", "*", "*")),
        *glob.glob(os.path.join(repo_root, "scripts", "*.py")),
        *glob.glob(os.path.join(repo_root, ".claude", "hooks", "*")),
    ]
    fixed = 0
    for fpath in candidates:
        if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
            continue
        with open(fpath, "rb") as f:
            content = f.read()
        if not content.endswith(b"\n"):
            with open(fpath, "ab") as f:
                f.write(b"\n")
            fixed += 1
    if fixed > 0:
        _vprint(f"  Fixed missing trailing newline in {fixed} file(s).")
    return fixed


def _run_upgrade_steps(src: str, repo_root: str, script_dir: str, tmpdir: str) -> dict:
    """Apply an extracted tarball to an existing install.

    Factored out of main() so integration tests can drive the full orchestration
    (mode detection, manifest translation, claude-only step gating, orphan
    removal, manifest write, VERSION stamp) against a fake src tree without
    hitting GitHub. The tarball download and rexec handoff stay in main().

    Returns a summary dict consumed by main() to render the final report.
    """
    install_mode = detect_install_mode(script_dir)
    if install_mode != "claude":
        _vprint(f"  Install mode: {install_mode}")
    manifest_rel = INSTALL_MODES[install_mode]["manifest_rel"]
    old_manifest = os.path.join(repo_root, manifest_rel)
    new_manifest = os.path.join(src, "MANIFEST")

    # In non-claude modes, the tarball's MANIFEST is claude-shaped; translate
    # to the local install layout before comparing so orphan detection doesn't
    # treat every file as {orphan, new}.
    translated_new_manifest = new_manifest
    if install_mode != "claude" and os.path.isfile(new_manifest):
        with open(new_manifest) as _f:
            _raw_files = json.load(_f)
        translated_files = translate_manifest_for_mode(_raw_files, install_mode)
        translated_new_manifest = os.path.join(tmpdir, "MANIFEST.translated")
        with open(translated_new_manifest, "w") as _f:
            json.dump(translated_files, _f, indent=2)
            _f.write("\n")

    orphan_count = 0
    if os.path.isfile(old_manifest) and os.path.isfile(translated_new_manifest):
        orphan_count = remove_orphans(old_manifest, translated_new_manifest, repo_root)
    elif not os.path.isfile(old_manifest):
        print("  No prior manifest found; skipping orphan removal (first upgrade with manifest support)")
    else:
        print("  Warning: new release has no MANIFEST file; skipping orphan removal")

    copy_bin_files(src, script_dir)
    # Skills, hooks, setup-path, settings.json merge, and review-commits
    # permissions are Claude-only concepts. Codex has no equivalents, so
    # skip them to avoid writing into a non-existent .claude/ layout.
    if install_mode == "claude":
        skill_count = copy_skills(src, repo_root)
        hook_count = copy_hooks(src, repo_root)
        override_setup_path(repo_root)
        hook_summary = merge_hook_registrations(src, repo_root)
        added_perms = ensure_review_commits_permissions(repo_root)
        for entry in added_perms:
            _vprint(f"  Added required permission: {entry}")
        prompt_count = 0
    else:
        skill_count = 0
        hook_count = 0
        hook_summary = {"registered": 0, "dedup_removed": 0, "permissions_added": 0}
        added_perms = []
        prompt_count = copy_prompts(src, repo_root)
    script_count = copy_scripts(src, repo_root)
    backfilled_keys = merge_config_defaults(src, repo_root, script_dir)

    # Run migrations using the newly installed binary. In quiet mode, capture
    # stdout so only the single-line schema summary is surfaced below.
    migrate_cmd = [os.path.join(script_dir, "tusk"), "migrate"]
    if _verbose:
        subprocess.run(migrate_cmd, check=True)
        migrate_summary = "ran"
    else:
        result = subprocess.run(migrate_cmd, check=True, capture_output=True, text=True)
        migrate_summary = (result.stdout or "ran").strip().splitlines()[-1]

    deprecated_count = remove_deprecated_files(repo_root)
    update_gitignore(script_dir)

    if os.path.isfile(translated_new_manifest):
        os.makedirs(os.path.dirname(old_manifest), exist_ok=True)
        shutil.copy2(translated_new_manifest, old_manifest)
        _vprint(f"  Updated {manifest_rel}")

    newline_fixes = fix_trailing_newlines(script_dir, repo_root)

    # Stamp VERSION last — ensures interrupted upgrades re-run next time
    shutil.copy2(os.path.join(src, "VERSION"), os.path.join(script_dir, "VERSION"))

    return {
        "install_mode": install_mode,
        "manifest_rel": manifest_rel,
        "orphan_count": orphan_count,
        "skill_count": skill_count,
        "hook_count": hook_count,
        "hook_summary": hook_summary,
        "added_perms": added_perms,
        "prompt_count": prompt_count,
        "script_count": script_count,
        "backfilled_keys": backfilled_keys,
        "migrate_summary": migrate_summary,
        "deprecated_count": deprecated_count,
        "newline_fixes": newline_fixes,
    }


def stage_and_commit(repo_root: str, manifest_path: str, remote_version: int) -> None:
    with open(manifest_path) as f:
        files = json.load(f)
    to_stage = [p for p in files if os.path.isfile(os.path.join(repo_root, p))]
    if to_stage:
        subprocess.run(
            ["git", "-C", repo_root, "add", "--force", "--"] + to_stage,
            check=True,
        )
    result = subprocess.run(
        ["git", "-C", repo_root, "diff", "--cached", "--quiet"]
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "-C", repo_root, "commit", "-m", f"Upgrade tusk to v{remote_version}"],
            check=True,
        )
        print(f"  Created commit: Upgrade tusk to v{remote_version}")
    else:
        print("  No changes to commit (working tree already up to date).")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Upgrade tusk from GitHub")
    parser.add_argument("repo_root", help="Absolute path to repo root")
    parser.add_argument("script_dir", help="Absolute path to script dir")
    parser.add_argument("--no-commit", action="store_true", help="Skip auto-commit")
    parser.add_argument("--force", action="store_true", help="Force upgrade even if same version")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show per-file detail; default is a compact summary.",
    )
    # Internal: set by a re-exec from a newly-extracted upgrader. Points at the
    # already-extracted src/ directory inside a tempdir we now own. Never set by
    # humans — hidden from --help.
    parser.add_argument("--_rexec-src", dest="rexec_src", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    repo_root = args.repo_root
    script_dir = args.script_dir

    # Refuse to run inside the tusk source repo. stage_and_commit does
    # `git add --force` on MANIFEST paths, several of which (.claude/skills/*)
    # are symlinks here and crash with "pathspec is beyond a symbolic link".
    # The upgrade is also a no-op — VERSION tracks HEAD and source files are
    # byte-identical to their own targets. git pull is the right tool.
    if is_source_repo(repo_root):
        print("This is the tusk source repo; use git pull to update.")
        return

    # Guard the hidden --_rexec-src flag: the tempdir we'd rmtree in finally is
    # derived from this path, so require it to live under the system tempdir.
    # Under normal operation only our own os.execv sets it, always to a
    # mkdtemp(prefix="tusk-upgrade-") subpath — anything else is misuse.
    if args.rexec_src:
        tmp_root = os.path.realpath(tempfile.gettempdir())
        rexec_real = os.path.realpath(args.rexec_src)
        try:
            common = os.path.commonpath([rexec_real, tmp_root])
        except ValueError:
            common = ""
        if common != tmp_root:
            raise SystemExit(
                f"Error: --_rexec-src must be a subpath of {tmp_root} "
                f"(got: {args.rexec_src})"
            )

    version_path = os.path.join(script_dir, "VERSION")
    try:
        local_version = int(Path(version_path).read_text().strip()) if os.path.exists(version_path) else 0
    except ValueError as e:
        raise SystemExit(f"Error: Could not parse local VERSION: {e}") from e

    if not args.rexec_src:
        print("Checking for updates...")

    latest_tag = get_latest_tag()
    remote_version = get_remote_version(latest_tag)

    if not args.force:
        if local_version == remote_version:
            print(f"Already up to date (version {local_version}).")
            return
        if local_version > remote_version:
            print(f"Warning: Local version ({local_version}) is ahead of remote ({remote_version}).")
            print("This may indicate a dev build or an unpublished release.")
            return

    if not args.rexec_src:
        print(f"Upgrading from version {local_version} → {remote_version}...")

    # Own a tempdir that survives a potential os.execv to a newer upgrader.
    # When re-exec'd we inherit ownership of the tempdir created by the outer
    # (older) upgrader — clean it up in our own finally.
    if args.rexec_src:
        src = args.rexec_src
        tmpdir = os.path.dirname(src.rstrip("/"))
    else:
        tmpdir = tempfile.mkdtemp(prefix="tusk-upgrade-")

    try:
        if not args.rexec_src:
            # Download and extract tarball
            tarball_path = os.path.join(tmpdir, "tusk.tar.gz")
            tarball_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{latest_tag}.tar.gz"
            tarball_data = fetch_bytes(tarball_url, timeout=DL_TIMEOUT)
            with open(tarball_path, "wb") as f:
                f.write(tarball_data)
            with tarfile.open(tarball_path) as tar:
                tar.extractall(tmpdir, filter="data")

            # Find extracted directory (tusk-v2, tusk-v3, etc.)
            extracted = [
                d for d in os.listdir(tmpdir)
                if os.path.isdir(os.path.join(tmpdir, d)) and d.startswith("tusk-")
            ]
            if not extracted:
                raise SystemExit("Error: Unexpected archive structure.")
            src = os.path.join(tmpdir, extracted[0])

            # Close the bootstrap gap: if the upgrader in the tarball differs
            # from the one currently running, hand off to the new copy so its
            # UX/behavior changes take effect on this same run — not the next.
            if _should_rexec(src, script_dir):
                new_script = os.path.join(src, "bin", "tusk-upgrade.py")
                argv = [sys.executable, new_script, repo_root, script_dir, "--_rexec-src", src]
                if args.no_commit:
                    argv.append("--no-commit")
                if args.force:
                    argv.append("--force")
                if args.verbose:
                    argv.append("--verbose")
                # os.execv does NOT return; the new process inherits tmpdir
                # and will rmtree it inside its own finally block.
                os.execv(sys.executable, argv)

        summary = _run_upgrade_steps(src, repo_root, script_dir, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    install_mode = summary["install_mode"]
    manifest_rel = summary["manifest_rel"]

    if not _verbose:
        hook_summary = summary["hook_summary"]
        print(f"  Skills       {summary['skill_count']} updated")
        print(f"  Hooks        {summary['hook_count']} updated"
              + (f", {hook_summary['registered']} registered" if hook_summary["registered"] else "")
              + (f", {hook_summary['dedup_removed']} dedup'd" if hook_summary["dedup_removed"] else ""))
        if summary["script_count"]:
            print(f"  Scripts      {summary['script_count']} updated")
        perms_total = hook_summary["permissions_added"] + len(summary["added_perms"])
        if perms_total:
            print(f"  Permissions  {perms_total} added")
        if summary["backfilled_keys"]:
            print(f"  Config       {len(summary['backfilled_keys'])} key(s) backfilled: {', '.join(summary['backfilled_keys'])}")
        print(f"  Migrations   {summary['migrate_summary']}")
        cleanup_bits = []
        if summary["orphan_count"]:
            cleanup_bits.append(f"{summary['orphan_count']} orphan(s)")
        if summary["deprecated_count"]:
            cleanup_bits.append(f"{summary['deprecated_count']} deprecated file(s)")
        if summary["newline_fixes"]:
            cleanup_bits.append(f"{summary['newline_fixes']} newline fix(es)")
        if cleanup_bits:
            print(f"  Cleanup      {', '.join(cleanup_bits)}")

    print()
    print(f"Upgrade complete (version {remote_version}).")

    # Safety net: ensure_review_commits_permissions should have added any missing
    # entries during the upgrade. If anything is still missing here, the write
    # likely failed (e.g. read-only filesystem) — surface a warning rather than
    # silently letting /review-commits break. Codex installs have no settings.json
    # (nor a /review-commits skill), so skip the check.
    if install_mode == "claude":
        missing = check_review_commits_permissions(repo_root)
        if missing:
            print()
            print("  Warning: The following permissions.allow entries are still missing from")
            print("  .claude/settings.json and are required for /review-commits to work:")
            print()
            for entry in missing:
                print(f'    "{entry}"')
            print()
            print("  Add these entries to the permissions.allow array in .claude/settings.json.")

    # Auto-commit
    if args.no_commit:
        print("  Skipping auto-commit (--no-commit flag set).")
        return

    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        )
        if result.returncode != 0:
            print("  Warning: Not inside a git repository — skipping auto-commit.")
            return
    except FileNotFoundError:
        print("  Warning: git not found — skipping auto-commit.")
        return

    manifest_path = os.path.join(repo_root, manifest_rel)
    if not os.path.isfile(manifest_path):
        print(f"  Warning: {manifest_rel} not found — skipping auto-commit.")
        return

    stage_and_commit(repo_root, manifest_path, remote_version)


if __name__ == "__main__":
    main()
