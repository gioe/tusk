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
import json
import os
import shutil
import ssl
import subprocess
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

def remove_orphans(old_manifest_path: str, new_manifest_path: str, repo_root: str) -> None:
    with open(old_manifest_path) as f:
        old_files = set(json.load(f))
    with open(new_manifest_path) as f:
        new_files = set(json.load(f))
    orphans = old_files - new_files
    for rel_path in sorted(orphans):
        full_path = os.path.join(repo_root, rel_path)
        if os.path.isfile(full_path):
            os.remove(full_path)
            print(f"  Removed orphan: {rel_path}")
            parent = os.path.dirname(full_path)
            try:
                os.rmdir(parent)
                print(f"  Removed empty dir: {os.path.relpath(parent, repo_root)}")
            except OSError:
                print(
                    f"  Kept non-empty dir (user files present): "
                    f"{os.path.relpath(parent, repo_root)}"
                )


def copy_bin_files(src: str, script_dir: str) -> None:
    """Copy CLI and support files; use atomic rename for the running tusk binary."""
    tusk_tmp = os.path.join(script_dir, "tusk.tmp")
    shutil.copy2(os.path.join(src, "bin", "tusk"), tusk_tmp)
    os.chmod(tusk_tmp, 0o755)
    os.replace(tusk_tmp, os.path.join(script_dir, "tusk"))
    for pyfile in Path(os.path.join(src, "bin")).glob("tusk-*.py"):
        shutil.copy2(str(pyfile), script_dir)
    shutil.copy2(
        os.path.join(src, "config.default.json"),
        os.path.join(script_dir, "config.default.json"),
    )
    shutil.copy2(
        os.path.join(src, "pricing.json"),
        os.path.join(script_dir, "pricing.json"),
    )
    print("  Updated CLI and support files")


def copy_skills(src: str, repo_root: str) -> None:
    skills_src = os.path.join(src, "skills")
    if not os.path.isdir(skills_src):
        return
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
        print(f"  Updated skill: {skill_name}")


def copy_scripts(src: str, repo_root: str) -> None:
    scripts_src = os.path.join(src, "scripts")
    if not os.path.isdir(scripts_src):
        return
    os.makedirs(os.path.join(repo_root, "scripts"), exist_ok=True)
    for script in Path(scripts_src).glob("*.py"):
        shutil.copy2(str(script), os.path.join(repo_root, "scripts"))
        print(f"  Updated scripts/{script.name}")


def copy_hooks(src: str, repo_root: str) -> None:
    hooks_src = os.path.join(src, ".claude", "hooks")
    if not os.path.isdir(hooks_src):
        return
    hooks_dest = os.path.join(repo_root, ".claude", "hooks")
    os.makedirs(hooks_dest, exist_ok=True)
    for hookfile in os.listdir(hooks_src):
        src_hook = os.path.join(hooks_src, hookfile)
        if not os.path.isfile(src_hook):
            continue
        dest_hook = os.path.join(hooks_dest, hookfile)
        shutil.copy2(src_hook, dest_hook)
        os.chmod(dest_hook, 0o755)
        print(f"  Updated hook: {hookfile}")


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


def merge_hook_registrations(src: str, repo_root: str) -> None:
    source_settings_path = os.path.join(src, ".claude", "settings.json")
    target_settings_path = os.path.join(repo_root, ".claude", "settings.json")

    if not os.path.isfile(source_settings_path):
        return

    try:
        with open(source_settings_path) as f:
            source_hooks = json.load(f).get("hooks", {})
    except json.JSONDecodeError as e:
        raise SystemExit(f"Error: Could not parse source settings.json: {e}") from e

    if os.path.exists(target_settings_path):
        try:
            with open(target_settings_path) as f:
                target_settings = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Error: Could not parse target settings.json: {e}") from e
    else:
        target_settings = {}

    target_hooks = target_settings.setdefault("hooks", {})

    for event_type, source_groups in source_hooks.items():
        target_groups = target_hooks.setdefault(event_type, [])
        existing_commands = set()
        for group in target_groups:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if cmd:
                    existing_commands.add(cmd)
        for group in source_groups:
            group_commands = [h.get("command", "") for h in group.get("hooks", [])]
            if not any(cmd in existing_commands for cmd in group_commands if cmd):
                target_groups.append(group)
                for cmd in group_commands:
                    if cmd:
                        print(f"  Registered hook: {cmd}")
            else:
                for cmd in group_commands:
                    if cmd:
                        print(f"  Hook already registered: {cmd}")

    with open(target_settings_path, "w") as f:
        json.dump(target_settings, f, indent=2)
        f.write("\n")


def remove_deprecated_files(repo_root: str) -> None:
    for rel in ["tusk/conventions.md", "tusk/dashboard.html", "tusk/tusk.db"]:
        full = os.path.join(repo_root, rel)
        if os.path.isfile(full):
            os.remove(full)
            print(f"  Removed deprecated file: {rel}")


def update_gitignore(script_dir: str) -> None:
    subprocess.run([os.path.join(script_dir, "tusk"), "update-gitignore"], check=True)


def fix_trailing_newlines(script_dir: str, repo_root: str) -> None:
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
        print(f"  Fixed missing trailing newline in {fixed} file(s).")


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
    args = parser.parse_args()

    repo_root = args.repo_root
    script_dir = args.script_dir

    version_path = os.path.join(script_dir, "VERSION")
    try:
        local_version = int(Path(version_path).read_text().strip()) if os.path.exists(version_path) else 0
    except ValueError as e:
        raise SystemExit(f"Error: Could not parse local VERSION: {e}") from e

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

    print(f"Upgrading from version {local_version} → {remote_version}...")

    with tempfile.TemporaryDirectory() as tmpdir:
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

        old_manifest = os.path.join(repo_root, ".claude", "tusk-manifest.json")
        new_manifest = os.path.join(src, "MANIFEST")

        # Remove orphaned files
        if os.path.isfile(old_manifest) and os.path.isfile(new_manifest):
            remove_orphans(old_manifest, new_manifest, repo_root)
        elif not os.path.isfile(old_manifest):
            print("  No prior manifest found; skipping orphan removal (first upgrade with manifest support)")
        else:
            print("  Warning: new release has no MANIFEST file; skipping orphan removal")

        copy_bin_files(src, script_dir)
        copy_skills(src, repo_root)
        copy_scripts(src, repo_root)
        copy_hooks(src, repo_root)
        override_setup_path(repo_root)
        merge_hook_registrations(src, repo_root)

        # Run migrations using the newly installed binary
        subprocess.run([os.path.join(script_dir, "tusk"), "migrate"], check=True)

        remove_deprecated_files(repo_root)
        update_gitignore(script_dir)

        if os.path.isfile(new_manifest):
            shutil.copy2(new_manifest, old_manifest)
            print("  Updated .claude/tusk-manifest.json")

        fix_trailing_newlines(script_dir, repo_root)

        # Stamp VERSION last — ensures interrupted upgrades re-run next time
        shutil.copy2(os.path.join(src, "VERSION"), os.path.join(script_dir, "VERSION"))

    print()
    print(f"Upgrade complete (version {remote_version}).")

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

    manifest_path = os.path.join(repo_root, ".claude", "tusk-manifest.json")
    if not os.path.isfile(manifest_path):
        print("  Warning: .claude/tusk-manifest.json not found — skipping auto-commit.")
        return

    stage_and_commit(repo_root, manifest_path, remote_version)


if __name__ == "__main__":
    main()
