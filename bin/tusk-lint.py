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
    """No raw sqlite3 usage outside bin/tusk."""
    violations = []
    exempt = {"bin/tusk", "CLAUDE.md", "README.md"}
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
             "WHERE t.status = 'Done' AND ac.is_completed = 0 "
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
    """Tasks with [Deferred] prefix but no expires_at set."""
    violations = []
    tusk_bin = os.path.join(root, "bin", "tusk")
    if not os.path.isfile(tusk_bin):
        tusk_bin = "tusk"
    try:
        result = subprocess.run(
            [tusk_bin, "-header", "-column",
             "SELECT id, summary FROM tasks "
             "WHERE summary LIKE '[Deferred]%' AND expires_at IS NULL AND status <> 'Done'"],
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


# ── Main ─────────────────────────────────────────────────────────────

RULES = [
    ("Rule 1: No raw sqlite3 usage", rule1_raw_sqlite3),
    ("Rule 2: SQL != operator", rule2_sql_not_equal),
    ("Rule 3: Hardcoded database path", rule3_hardcoded_db_path),
    ("Rule 4: Manual quote escaping", rule4_manual_quote_escaping),
    ("Rule 5: Done without closed_reason", rule5_done_without_closed_reason),
    ("Rule 6: Done with incomplete acceptance criteria", rule6_done_incomplete_criteria),
    ("Rule 7: config.default.json keys match KNOWN_KEYS", rule7_config_keys_match_known_keys),
    ("Rule 8: Orphaned tusk-*.py scripts (in bin/ but not in dispatcher)", rule8_orphaned_python_scripts),
    ("Rule 9: Deferred tasks missing expires_at", rule9_deferred_missing_expiry),
    ("Rule 10: acceptance_criteria with verification_spec but criterion_type='manual'", rule10_criteria_type_mismatch),
    ("Rule 11: SKILL.md frontmatter validation", rule11_skill_frontmatter),
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

    for name, check_fn in RULES:
        violations = check_fn(root)
        print(name)
        if violations:
            total_violations += len(violations)
            rules_with_violations += 1
            print(f"  WARN — {len(violations)} violation{'s' if len(violations) != 1 else ''}")
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
