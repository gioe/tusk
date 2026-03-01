#!/usr/bin/env python3
"""
tusk-lint — run tusk convention checks non-interactively.

Usage: tusk-lint.py <repo_root>

Checks the tusk codebase against Key Conventions from CLAUDE.md.
Prints results grouped by rule and exits with status 1 if any violations found.
"""

import json
import os
import re
import shutil
import subprocess
import sys


def find_files(root, dirs, extensions):
    """Yield (relative_path, full_path) for files matching extensions in dirs."""
    for d in dirs:
        dirpath = os.path.join(root, d)
        if not os.path.isdir(dirpath):
            continue
        for dirroot, _, filenames in os.walk(dirpath):
            for fname in filenames:
                if any(fname.endswith(ext) for ext in extensions):
                    full = os.path.join(dirroot, fname)
                    rel = os.path.relpath(full, root)
                    yield rel, full


def read_lines(path):
    """Read file lines, returning list of (line_number, line_text)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(enumerate(f.readlines(), 1))
    except OSError:
        return []


# ── Self-exemption ───────────────────────────────────────────────────

SELF_FILES = {
    "skills/lint-conventions/SKILL.md",
    ".claude/skills/lint-conventions/SKILL.md",
}


def is_self(rel):
    return rel in SELF_FILES


# ── Rule implementations ────────────────────────────────────────────

def rule1_raw_sqlite3(root):
    """No raw sqlite3 CLI usage outside bin/tusk.

    Skills/scripts: flag any 'sqlite3 ' reference (shell CLI invocation).
    bin/tusk-*.py: flag only subprocess calls to sqlite3 (e.g. subprocess.run(['sqlite3',...]))
                   — Python's native sqlite3 module (sqlite3.connect, etc.) is allowed for
                   read-only lookups in bin scripts.
    """
    violations = []
    exempt = {"bin/tusk", "CLAUDE.md", "README.md"}

    # Skills and plain scripts: flag any sqlite3 CLI invocation
    for rel, full in find_files(root, ["skills", "scripts"], [".md", ".sh", ".py"]):
        if is_self(rel) or any(rel.endswith(e) or rel == e for e in exempt):
            continue
        for lineno, line in read_lines(full):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "sqlite3 " not in line or "bin/tusk" in line:
                continue
            # Skip if sqlite3 only appears in a trailing comment
            comment_pos = line.find("#")
            if comment_pos >= 0:
                before_comment = line[:comment_pos]
                if "sqlite3 " not in before_comment:
                    continue
            violations.append(f"  {rel}:{lineno}: {line.rstrip()}")

    # bin/tusk-*.py: flag only subprocess-based sqlite3 CLI calls; allow Python's sqlite3 module
    import re as _re
    _subprocess_sqlite3 = _re.compile(r'subprocess[^#\n]*["\']sqlite3["\']')
    bin_exempt = exempt | {"bin/tusk-lint.py"}
    for rel, full in find_files(root, ["bin"], [".py"]):
        if not _re.match(r"bin/tusk-.+\.py$", rel):
            continue
        if rel in bin_exempt:
            continue
        for lineno, line in read_lines(full):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _subprocess_sqlite3.search(line):
                violations.append(f"  {rel}:{lineno}: {line.rstrip()}")

    return violations


def rule2_sql_not_equal(root):
    """SQL != operator in bash/skill contexts."""
    violations = []
    exempt_patterns = ["CLAUDE.md", "README.md"]
    for rel, full in find_files(root, ["skills", "bin", "scripts"], [".md", ".sh"]):
        if is_self(rel):
            continue
        if any(p in rel for p in exempt_patterns):
            continue
        # Skip Python files — Python != is fine
        if rel.endswith(".py"):
            continue
        for lineno, line in read_lines(full):
            if "!=" not in line:
                continue
            # Only flag lines that look like SQL (WHERE, AND, OR, HAVING, CHECK, WHEN)
            upper = line.upper()
            sql_keywords = ["WHERE", "AND ", "OR ", "HAVING", "CHECK", "WHEN ", "SET "]
            if any(kw in upper for kw in sql_keywords):
                violations.append(f"  {rel}:{lineno}: {line.rstrip()}")
    return violations


def rule3_hardcoded_db_path(root):
    """Hardcoded database path (tusk/tasks.db)."""
    violations = []
    exempt = {"CLAUDE.md", "README.md", "install.sh", "bin/tusk", "bin/tusk-lint.py"}
    for rel, full in find_files(root, ["skills", "scripts", "bin"], [".md", ".sh", ".py"]):
        if is_self(rel) or any(rel == e or rel.endswith("/" + e) for e in exempt):
            continue
        for lineno, line in read_lines(full):
            if "tusk/tasks.db" in line:
                violations.append(f"  {rel}:{lineno}: {line.rstrip()}")
    return violations


def rule4_manual_quote_escaping(root):
    """Manual single-quote escaping instead of tusk sql-quote."""
    violations = []
    exempt = {"bin/tusk"}
    pat1 = re.compile(r"sed.*s/'")
    pat2 = re.compile(r"replace.*'.*''")

    for rel, full in find_files(root, ["skills", "scripts"], [".md", ".sh", ".py"]):
        if is_self(rel) or rel in exempt:
            continue
        for lineno, line in read_lines(full):
            if "sql_quote" in line or "sql-quote" in line:
                continue
            if pat1.search(line) or pat2.search(line):
                violations.append(f"  {rel}:{lineno}: {line.rstrip()}")
    return violations


def rule5_done_without_closed_reason(root):
    """Setting status='Done' without closed_reason."""
    violations = []
    exempt = {"bin/tusk-lint.py"}
    done_re = re.compile(r"status\s*=\s*'Done'", re.IGNORECASE)
    # Matches UPDATE or INSERT that would actually *set* the status
    write_re = re.compile(r"(?<!-)\b(UPDATE|INSERT)\b", re.IGNORECASE)
    select_re = re.compile(r"\bSELECT\b", re.IGNORECASE)

    for rel, full in find_files(root, ["skills", "scripts", "bin"], [".md", ".sh", ".py"]):
        if is_self(rel) or rel in exempt:
            continue
        lines = read_lines(full)
        for i, (lineno, line) in enumerate(lines):
            if not done_re.search(line):
                continue

            # Fast path: if the line itself is a SELECT (no write keywords), skip
            if select_re.search(line) and not write_re.search(line):
                continue

            # Check surrounding context (same line + 15 lines before)
            # to determine the SQL statement type
            context_start = max(0, i - 15)
            context_end = min(len(lines), i + 6)
            context = "".join(l for _, l in lines[context_start:context_end])

            # Skip if this is a SELECT query (read-only, not setting status)
            if select_re.search(context) and not write_re.search(context):
                continue

            if "closed_reason" not in context:
                violations.append(f"  {rel}:{lineno}: {line.rstrip()}")
    return violations


def rule6_done_incomplete_criteria(root):
    """Tasks marked Done with incomplete acceptance criteria."""
    violations = []
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        # Installed projects: tusk is on PATH via .claude/bin/
        tusk_bin = "tusk"
    try:
        result = subprocess.run(
            [tusk_bin, "-header", "-column",
             "SELECT t.id, t.summary, COUNT(ac.id) AS incomplete "
             "FROM tasks t "
             "JOIN acceptance_criteria ac ON ac.task_id = t.id "
             "WHERE t.status = 'Done' AND ac.is_completed = 0 AND ac.is_deferred = 0 "
             "GROUP BY t.id"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("id") and not line.startswith("--"):
                violations.append(f"  {line}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Skip rule if tusk CLI is unavailable
    return violations


def rule9_deferred_missing_expiry(root):
    """Deferred tasks (is_deferred=1) with no expires_at set."""
    violations = []
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"
    try:
        result = subprocess.run(
            [tusk_bin, "-header", "-column",
             "SELECT id, summary FROM tasks "
             "WHERE is_deferred = 1 AND expires_at IS NULL AND status <> 'Done'"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("id") and not line.startswith("--"):
                violations.append(f"  {line}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Skip rule if tusk CLI is unavailable
    return violations


def rule10_criteria_type_mismatch(root):
    """acceptance_criteria with verification_spec set but criterion_type='manual'."""
    violations = []
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"
    try:
        result = subprocess.run(
            [tusk_bin, "-header", "-column",
             "SELECT ac.id, ac.task_id, ac.criterion FROM acceptance_criteria ac "
             "WHERE ac.verification_spec IS NOT NULL AND ac.criterion_type = 'manual'"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("id") and not line.startswith("--"):
                violations.append(f"  {line}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Skip rule if tusk CLI is unavailable
    return violations


def rule8_orphaned_python_scripts(root):
    """tusk-*.py files on disk not referenced in bin/tusk or other tusk-*.py files."""
    violations = []
    bin_dir = os.path.join(root, "bin")
    tusk_path = os.path.join(root, "bin", "tusk")

    if not os.path.isfile(tusk_path):
        return []

    try:
        with open(tusk_path, encoding="utf-8") as f:
            tusk_content = f.read()
    except OSError:
        return []

    try:
        disk_scripts = sorted(
            f for f in os.listdir(bin_dir)
            if re.match(r"^tusk-.+\.py$", f)
        )
    except OSError:
        return []

    for script in disk_scripts:
        if script in tusk_content:
            continue
        # Allow library files referenced by other tusk-*.py scripts
        used_as_lib = False
        for other in disk_scripts:
            if other == script:
                continue
            try:
                with open(os.path.join(bin_dir, other), encoding="utf-8") as f:
                    if script in f.read():
                        used_as_lib = True
                        break
            except OSError:
                pass
        if not used_as_lib:
            violations.append(
                f"  bin/{script}: exists on disk but is not referenced in bin/tusk dispatcher"
            )
    return violations


def rule7_config_keys_match_known_keys(root):
    """config.default.json top-level keys must match KNOWN_KEYS in cmd_validate."""
    violations = []

    # Parse config.default.json
    config_path = os.path.join(root, "config.default.json")
    if not os.path.isfile(config_path):
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        config_keys = set(cfg.keys())
    except (OSError, json.JSONDecodeError):
        return []

    # Extract KNOWN_KEYS from bin/tusk
    tusk_path = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_path):
        return []
    known_keys = set()
    known_keys_re = re.compile(r"KNOWN_KEYS\s*=\s*\{([^}]+)\}")
    try:
        with open(tusk_path, encoding="utf-8") as f:
            content = f.read()
        m = known_keys_re.search(content)
        if m:
            # Parse the set literal: 'key1', 'key2', ...
            for key_match in re.finditer(r"'([^']+)'", m.group(1)):
                known_keys.add(key_match.group(1))
    except OSError:
        return []

    if not known_keys:
        return []

    # Check both directions
    in_config_not_known = config_keys - known_keys
    in_known_not_config = known_keys - config_keys

    for k in sorted(in_config_not_known):
        violations.append(
            f"  config.default.json has key \"{k}\" not in KNOWN_KEYS (bin/tusk cmd_validate)"
        )
    for k in sorted(in_known_not_config):
        violations.append(
            f"  KNOWN_KEYS (bin/tusk cmd_validate) has \"{k}\" not in config.default.json"
        )

    return violations


def rule11_skill_frontmatter(root):
    """skills/*/SKILL.md must have valid YAML frontmatter with name, description, and allowed-tools."""
    violations = []
    skills_dir = os.path.join(root, "skills")
    if not os.path.isdir(skills_dir):
        return []

    for skill_name in sorted(os.listdir(skills_dir)):
        skill_dir = os.path.join(skills_dir, skill_name)
        if not os.path.isdir(skill_dir):
            continue
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue

        rel = os.path.relpath(skill_md, root)
        lines = read_lines(skill_md)

        if not lines:
            violations.append(f"  {rel}: file is empty or unreadable")
            continue

        # Check for opening ---
        first_line = lines[0][1].strip()
        if first_line != "---":
            violations.append(f"  {rel}: missing YAML frontmatter (file must start with ---)")
            continue

        # Find closing ---
        frontmatter_lines = []
        closing_found = False
        for _lineno, line in lines[1:]:
            if line.strip() == "---":
                closing_found = True
                break
            frontmatter_lines.append(line)

        if not closing_found:
            violations.append(f"  {rel}: YAML frontmatter not closed (missing second ---)")
            continue

        # Parse frontmatter key-value pairs
        frontmatter = {}
        for line in frontmatter_lines:
            m = re.match(r"^([^:]+):\s*(.*)$", line.strip())
            if m:
                frontmatter[m.group(1).strip()] = m.group(2).strip()

        # Check required fields
        for field in ["name", "description", "allowed-tools"]:
            if field not in frontmatter:
                violations.append(f"  {rel}: missing required frontmatter field '{field}'")

        # Check name matches directory name
        if "name" in frontmatter and frontmatter["name"] != skill_name:
            violations.append(
                f"  {rel}: frontmatter 'name' ({frontmatter['name']!r}) does not match directory name ({skill_name!r})"
            )

    return violations


def rule12_python_syntax(root):
    """Python syntax check for all bin/tusk-*.py files via py_compile."""
    violations = []

    # Guard: skip rule if python3 is not available (which python3)
    if not shutil.which("python3"):
        return []

    bin_dir = os.path.join(root, "bin")
    if not os.path.isdir(bin_dir):
        return []

    try:
        scripts = sorted(
            f for f in os.listdir(bin_dir)
            if re.match(r"^tusk-.+\.py$", f)
        )
    except OSError:
        return []

    for script in scripts:
        full = os.path.join(bin_dir, script)
        rel = os.path.relpath(full, root)
        try:
            result = subprocess.run(
                ["python3", "-m", "py_compile", full],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                # Extract line number from "File ..., line N" in the traceback
                line_match = re.search(r"line (\d+)", err)
                line_info = f":{line_match.group(1)}" if line_match else ":(unknown line)"
                # Surface the last (most informative) line of the error
                last_line = err.splitlines()[-1] if err else "SyntaxError"
                violations.append(f"  {rel}{line_info}: {last_line}")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # Skip file if py_compile invocation fails

    return violations


def rule14_deferred_prefix_mismatch(root):
    """Open tasks where is_deferred flag and [Deferred] summary prefix disagree."""
    violations = []
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"
    try:
        result = subprocess.run(
            [tusk_bin, "-header", "-column",
             "SELECT id, is_deferred, summary FROM tasks "
             "WHERE status <> 'Done' AND ("
             "  (summary LIKE '[Deferred]%' AND is_deferred = 0) OR "
             "  (summary NOT LIKE '[Deferred]%' AND is_deferred = 1)"
             ")"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("id") and not line.startswith("--"):
                violations.append(f"  {line}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Skip rule if tusk CLI is unavailable
    return violations


def rule15_big_bang_commits(root):
    """In Progress tasks where all completed non-deferred criteria share one commit_hash.

    Advisory only — fires when 2+ eligible criteria exist and all share a single hash.
    Partial grouping (some criteria on one hash, others on another) is NOT flagged.
    Tasks with zero or one eligible criterion are NOT flagged.
    """
    violations = []
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"
    try:
        result = subprocess.run(
            [tusk_bin, "-header", "-column",
             "SELECT t.id, MIN(t.summary) AS summary, COUNT(ac.id) AS criteria_count "
             "FROM tasks t "
             "JOIN acceptance_criteria ac ON ac.task_id = t.id "
             "WHERE t.status = 'In Progress' "
             "  AND ac.is_completed = 1 "
             "  AND ac.is_deferred = 0 "
             "  AND ac.commit_hash IS NOT NULL "
             "GROUP BY t.id "
             "HAVING COUNT(ac.id) > 1 AND COUNT(DISTINCT ac.commit_hash) = 1"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("id") and not line.startswith("--"):
                violations.append(f"  {line}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Skip rule if tusk CLI is unavailable
    return violations


def _version_bump_check(root, path_re, label):
    """Shared helper: check if files matching path_re were modified without a VERSION bump.

    Advisory only — caller is responsible for not counting violations toward exit code.
    """
    violations = []

    if not shutil.which("git"):
        return []

    # Verify we're inside a git repo
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5, cwd=root,
        )
        if r.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    def read_version():
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return "unknown"

    # --- Part A: Uncommitted changes (staged or unstaged) ---
    dirty_files = []
    version_dirty = False
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=root,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if len(line) < 4:
                    continue
                xy = line[:2]
                path = line[3:].strip()
                # Handle renamed files: "R old -> new"
                if " -> " in path:
                    path = path.split(" -> ")[-1]
                # Exclude untracked files (??) — they're not yet part of the project
                if xy.strip() and xy != "??" and path == "VERSION":
                    version_dirty = True
                if xy.strip() and xy != "??" and path_re.match(path):
                    dirty_files.append(path)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if dirty_files and not version_dirty:
        ver = read_version()
        for s in sorted(dirty_files):
            violations.append(f"  Uncommitted: VERSION={ver}, {label} modified without VERSION bump: {s}")

    # --- Part B: Committed changes since last VERSION bump ---
    # Skip if VERSION is currently dirty (user is already in the process of bumping it)
    if not version_dirty:
        try:
            r = subprocess.run(
                ["git", "log", "-1", "--format=%H", "--", "VERSION"],
                capture_output=True, text=True, timeout=5, cwd=root,
            )
            last_ver_commit = r.stdout.strip() if r.returncode == 0 else ""
            if last_ver_commit:
                r2 = subprocess.run(
                    ["git", "diff", "--name-only", f"{last_ver_commit}..HEAD"],
                    capture_output=True, text=True, timeout=5, cwd=root,
                )
                if r2.returncode == 0:
                    changed = r2.stdout.splitlines()
                    committed_files = [p for p in changed if path_re.match(p)]
                    # No need to check if VERSION is in the diff — last_ver_commit is by
                    # definition the most recent commit that touched VERSION, so nothing
                    # between it and HEAD can contain another VERSION change.
                    if committed_files:
                        ver = read_version()
                        for s in sorted(committed_files):
                            violations.append(
                                f"  Committed since last VERSION bump: VERSION={ver}, {label} modified without VERSION bump: {s}"
                            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return violations


def rule13_version_bump_missing(root):
    """bin/tusk-*.py modified in working tree or recent commits without VERSION bump.

    Advisory only — violations are printed but do not contribute to the exit code.
    """
    return _version_bump_check(root, re.compile(r"^bin/tusk-.+\.py$"), "script")


def rule20_skills_version_bump_missing(root):
    """skills/ file modified in working tree or recent commits without VERSION bump.

    Advisory only — violations are printed but do not contribute to the exit code.
    skills-internal/ is excluded: those files are not distributed to target projects.
    """
    if not os.path.isfile(os.path.join(root, "bin", "tusk")):
        return []
    return _version_bump_check(root, re.compile(r"^skills/"), "skill")


# ── DB-backed rules ──────────────────────────────────────────────────

def _db_path_from_root(root):
    """Resolve the tusk DB path by calling 'tusk path'. Returns path str or None."""
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"
    try:
        r = subprocess.run([tusk_bin, "path"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            p = r.stdout.strip()
            if p and os.path.isfile(p):
                return p
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _load_lint_rules(root, is_blocking):
    """Load lint_rules rows from the DB filtered by is_blocking. Returns [] on any failure."""
    import sqlite3 as _sqlite3
    db_path = _db_path_from_root(root)
    if not db_path:
        return []
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, grep_pattern, file_glob, message FROM lint_rules"
                " WHERE is_blocking = ? ORDER BY id",
                (1 if is_blocking else 0,),
            ).fetchall()
            return [dict(r) for r in rows]
        except _sqlite3.OperationalError:
            return []  # lint_rules table not yet created (pre-migration DB)
        finally:
            conn.close()
    except Exception:
        return []


def _run_lint_rules(root, rules):
    """Run a list of DB rule dicts as grep checks. Returns a list of violation strings."""
    import glob as _glob
    violations = []
    for rule in rules:
        rid = rule["id"]
        pattern = rule["grep_pattern"]
        file_glob = rule["file_glob"]
        message = rule["message"]
        try:
            matching = _glob.glob(os.path.join(root, file_glob), recursive=True)
        except Exception:
            continue
        for filepath in sorted(matching):
            if not os.path.isfile(filepath):
                continue
            try:
                result = subprocess.run(
                    ["grep", "-nE", pattern, filepath],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    rel = os.path.relpath(filepath, root)
                    for line in result.stdout.strip().splitlines():
                        violations.append(f"  [rule {rid}] {rel}:{line} — {message}")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
    return violations


def rule16_db_rules_blocking(root):
    """DB-backed lint rules with is_blocking=1 (count toward exit code)."""
    rules = _load_lint_rules(root, is_blocking=True)
    return _run_lint_rules(root, rules)


def rule17_db_rules_advisory(root):
    """DB-backed lint rules with is_blocking=0 (advisory warnings only)."""
    rules = _load_lint_rules(root, is_blocking=False)
    return _run_lint_rules(root, rules)


def rule18_manifest_drift(root):
    """MANIFEST file is out of sync with files distributed by install.sh."""
    import glob as _glob

    # This rule is only meaningful in the tusk source repo.  Target projects
    # don't have a MANIFEST or a bin/tusk shell script.  Mirror rule8's guard.
    if not os.path.isfile(os.path.join(root, "bin", "tusk")):
        return []

    manifest_path = os.path.join(root, "MANIFEST")
    if not os.path.isfile(manifest_path):
        return ["  MANIFEST file not found — update MANIFEST to match the current source tree"]

    try:
        with open(manifest_path, encoding="utf-8") as f:
            on_disk = set(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"  MANIFEST could not be parsed: {exc}"]

    # Generate expected manifest using the same logic as install.sh section 4c
    # Scripts that are only meaningful in the tusk source repo — not distributed.
    # Canonical source: bin/dist-excluded.txt (also read by tusk-generate-manifest.py and install.sh).
    _dist_excl_path = os.path.join(root, "bin", "dist-excluded.txt")
    try:
        with open(_dist_excl_path, encoding="utf-8") as _f:
            _dist_excluded = {line.strip() for line in _f if line.strip()}
    except OSError as exc:
        return [f"  bin/dist-excluded.txt could not be read: {exc}"]

    expected = []

    expected.append(".claude/bin/tusk")

    for p in sorted(_glob.glob(os.path.join(root, "bin", "tusk-*.py"))):
        if os.path.basename(p) in _dist_excluded:
            continue
        expected.append(".claude/bin/" + os.path.basename(p))

    for name in ["config.default.json", "VERSION", "pricing.json"]:
        expected.append(".claude/bin/" + name)

    for skill_dir in sorted(_glob.glob(os.path.join(root, "skills", "*/"))):
        skill_name = os.path.basename(skill_dir.rstrip("/"))
        for fname in sorted(os.listdir(skill_dir)):
            full = os.path.join(skill_dir, fname)
            if os.path.isfile(full):
                expected.append(".claude/skills/" + skill_name + "/" + fname)

    hooks_src = os.path.join(root, ".claude", "hooks")
    if os.path.isdir(hooks_src):
        for fname in sorted(os.listdir(hooks_src)):
            full = os.path.join(hooks_src, fname)
            if os.path.isfile(full):
                expected.append(".claude/hooks/" + fname)

    expected_set = set(expected)
    violations = []
    for path in sorted(expected_set - on_disk):
        violations.append(f"  MANIFEST: missing '{path}' (in source tree but not in MANIFEST)")
    for path in sorted(on_disk - expected_set):
        violations.append(f"  MANIFEST: extra '{path}' (in MANIFEST but not in source tree)")
    return violations


def rule19_tusk_manifest_json_sync(root):
    """.claude/tusk-manifest.json must have identical entries to MANIFEST."""
    # This rule is only meaningful in the tusk source repo.  Target projects
    # don't have a MANIFEST file.  Mirror rule8's guard.
    if not os.path.isfile(os.path.join(root, "bin", "tusk")):
        return []

    manifest_path = os.path.join(root, "MANIFEST")
    tusk_manifest_path = os.path.join(root, ".claude", "tusk-manifest.json")

    if not os.path.isfile(manifest_path):
        return ["  MANIFEST file not found"]

    if not os.path.isfile(tusk_manifest_path):
        return ["  .claude/tusk-manifest.json file not found"]

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = set(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"  MANIFEST could not be parsed: {exc}"]

    try:
        with open(tusk_manifest_path, encoding="utf-8") as f:
            tusk_manifest = set(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"  .claude/tusk-manifest.json could not be parsed: {exc}"]

    violations = []
    for path in sorted(manifest - tusk_manifest):
        violations.append(
            f"  MANIFEST has '{path}' but .claude/tusk-manifest.json does not"
            " (run bin/tusk-generate-manifest.py to regenerate both files)"
        )
    for path in sorted(tusk_manifest - manifest):
        violations.append(
            f"  .claude/tusk-manifest.json has '{path}' but MANIFEST does not"
            " (run bin/tusk-generate-manifest.py to regenerate both files)"
        )
    return violations


# ── Main ─────────────────────────────────────────────────────────────

# Each entry: (display_name, check_function, advisory)
# advisory=True  → violations are printed but do NOT count toward exit code
# advisory=False → violations count toward the non-zero exit code
RULES = [
    ("Rule 1: No raw sqlite3 usage", rule1_raw_sqlite3, False),
    ("Rule 2: SQL != operator", rule2_sql_not_equal, False),
    ("Rule 3: Hardcoded database path", rule3_hardcoded_db_path, False),
    ("Rule 4: Manual quote escaping", rule4_manual_quote_escaping, False),
    ("Rule 5: Done without closed_reason", rule5_done_without_closed_reason, False),
    ("Rule 6: Done with incomplete acceptance criteria", rule6_done_incomplete_criteria, False),
    ("Rule 7: config.default.json keys match KNOWN_KEYS", rule7_config_keys_match_known_keys, False),
    ("Rule 8: Orphaned tusk-*.py scripts (in bin/ but not in dispatcher)", rule8_orphaned_python_scripts, False),
    ("Rule 9: Deferred tasks missing expires_at", rule9_deferred_missing_expiry, False),
    ("Rule 10: acceptance_criteria with verification_spec but criterion_type='manual'", rule10_criteria_type_mismatch, False),
    ("Rule 11: SKILL.md frontmatter validation", rule11_skill_frontmatter, False),
    ("Rule 12: Python syntax check (py_compile) for bin/tusk-*.py", rule12_python_syntax, False),
    ("Rule 13: bin/tusk-*.py modified without VERSION bump (advisory)", rule13_version_bump_missing, True),
    ("Rule 14: is_deferred flag / [Deferred] prefix mismatch (advisory)", rule14_deferred_prefix_mismatch, True),
    ("Rule 15: Big-bang commits (all criteria on one commit) (advisory)", rule15_big_bang_commits, True),
    ("Rule 16: DB-backed blocking lint rules", rule16_db_rules_blocking, False),
    ("Rule 17: DB-backed advisory lint rules (advisory)", rule17_db_rules_advisory, True),
    ("Rule 18: MANIFEST drift from source tree", rule18_manifest_drift, False),
    ("Rule 19: .claude/tusk-manifest.json out of sync with MANIFEST", rule19_tusk_manifest_json_sync, False),
    ("Rule 20: skills/ file modified without VERSION bump (advisory)", rule20_skills_version_bump_missing, True),
]


def main():
    if len(sys.argv) < 2:
        print("Usage: tusk-lint.py <repo_root>", file=sys.stderr)
        sys.exit(2)

    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(2)

    total_violations = 0
    rules_with_violations = 0

    print("=== Lint Conventions Report ===")
    print()

    for name, check_fn, advisory in RULES:
        violations = check_fn(root)
        print(name)
        if violations:
            if not advisory:
                total_violations += len(violations)
                rules_with_violations += 1
            label = "WARN [ADVISORY]" if advisory else "WARN"
            print(f"  {label} — {len(violations)} violation{'s' if len(violations) != 1 else ''}")
            for v in violations:
                print(v)
        else:
            print("  PASS — no violations")
        print()

    if total_violations:
        print(f"=== Summary: {total_violations} violation{'s' if total_violations != 1 else ''} across {rules_with_violations} rule{'s' if rules_with_violations != 1 else ''} ===")
        sys.exit(1)
    else:
        print("=== Summary: no violations ===")
        sys.exit(0)


if __name__ == "__main__":
    main()
