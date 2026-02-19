#!/usr/bin/env python3
"""
tusk-token-audit — analyze skill token consumption and surface optimization opportunities.

Usage: tusk-token-audit.py <repo_root> [--summary | --json]

Scans skills/, skills-internal/, and .claude/skills/ for skill directories and
reports five analysis categories: size census, companion file analysis, SQL
anti-patterns, redundancy detection, and narrative density.

Exit codes:
  0  Success
  1  No skills found
  2  Usage error
"""

import json
import os
import re
import sys

TOKENS_PER_LINE = 10  # rough estimate

# ── File helpers (from tusk-lint.py pattern) ─────────────────────────


def find_skill_dirs(root):
    """Yield (skill_name, skill_dir, source_label) for each discovered skill."""
    seen = set()
    for source_dir, label in [
        (os.path.join(root, "skills"), "skills"),
        (os.path.join(root, "skills-internal"), "skills-internal"),
        (os.path.join(root, ".claude", "skills"), ".claude/skills"),
    ]:
        if not os.path.isdir(source_dir):
            continue
        for entry in sorted(os.listdir(source_dir)):
            entry_path = os.path.join(source_dir, entry)
            # Resolve symlinks to avoid double-counting
            real_path = os.path.realpath(entry_path)
            if not os.path.isdir(real_path):
                continue
            if real_path in seen:
                continue
            seen.add(real_path)
            skill_md = os.path.join(real_path, "SKILL.md")
            if os.path.isfile(skill_md):
                yield entry, real_path, label


def read_lines(path):
    """Read file, returning list of (line_number, line_text)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(enumerate(f.readlines(), 1))
    except OSError:
        return []


def count_lines(path):
    """Count lines in a file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def list_files_in_dir(dirpath):
    """List all files in a directory (non-recursive)."""
    try:
        return [f for f in os.listdir(dirpath) if os.path.isfile(os.path.join(dirpath, f))]
    except OSError:
        return []


# ── Category 1: Skill Size Census ───────────────────────────────────


def analyze_size_census(skills):
    """Count lines in SKILL.md + companions per skill, estimate tokens."""
    results = []
    for name, dirpath, source in skills:
        files = list_files_in_dir(dirpath)
        skill_md = os.path.join(dirpath, "SKILL.md")
        skill_lines = count_lines(skill_md)
        companion_lines = 0
        companions = []
        for f in files:
            if f == "SKILL.md":
                continue
            fpath = os.path.join(dirpath, f)
            lc = count_lines(fpath)
            companion_lines += lc
            companions.append((f, lc))
        total_lines = skill_lines + companion_lines
        results.append({
            "name": name,
            "source": source,
            "skill_md_lines": skill_lines,
            "companion_lines": companion_lines,
            "total_lines": total_lines,
            "estimated_tokens": total_lines * TOKENS_PER_LINE,
            "companions": companions,
        })
    results.sort(key=lambda x: x["total_lines"], reverse=True)
    return results


# ── Category 2: Companion File Analysis ─────────────────────────────

CONDITIONAL_KEYWORDS = re.compile(
    r"\b(if|when|only|skip|unless|otherwise|conditionally|for .* subcommand)\b",
    re.IGNORECASE,
)


def analyze_companions(skills):
    """Find Read file: directives, measure companion sizes, classify loading."""
    results = []
    for name, dirpath, source in skills:
        files = list_files_in_dir(dirpath)
        all_lines = {}
        for f in files:
            all_lines[f] = read_lines(os.path.join(dirpath, f))

        read_directives = []
        for f, lines in all_lines.items():
            for i, (lineno, line) in enumerate(lines):
                if "Read file:" not in line:
                    continue
                # Extract target filename
                match = re.search(r"Read file:\s*(.+)", line.strip())
                if not match:
                    continue
                target = match.group(1).strip().rstrip("`")

                # Classify as conditional or unconditional
                # Look backward up to 5 lines for conditional keywords
                context_start = max(0, i - 5)
                context = " ".join(l for _, l in lines[context_start:i + 1])
                is_conditional = bool(CONDITIONAL_KEYWORDS.search(context))

                # Resolve target size if it's a local companion
                target_basename = os.path.basename(target)
                target_lines = 0
                if target_basename in [fn for fn in files if fn != "SKILL.md"]:
                    target_lines = count_lines(os.path.join(dirpath, target_basename))

                read_directives.append({
                    "source_file": f,
                    "line": lineno,
                    "target": target,
                    "target_lines": target_lines,
                    "target_tokens": target_lines * TOKENS_PER_LINE,
                    "conditional": is_conditional,
                })

        if read_directives:
            results.append({
                "skill": name,
                "directives": read_directives,
            })
    return results


# ── Category 3: SQL Anti-Patterns ───────────────────────────────────

SELECT_STAR_RE = re.compile(r"\bSELECT\s+\*\b", re.IGNORECASE)
DESC_IN_SELECT_RE = re.compile(
    r"\bSELECT\b[^;]*\bdescription\b", re.IGNORECASE
)
TUSK_SETUP_RE = re.compile(r"\btusk\s+setup\b")
HEADER_COLUMN_RE = re.compile(r"-header\s+-column")


def analyze_sql_antipatterns(skills):
    """Grep code blocks for SQL anti-patterns."""
    results = []
    for name, dirpath, source in skills:
        findings = []
        files = list_files_in_dir(dirpath)
        for f in files:
            lines = read_lines(os.path.join(dirpath, f))
            in_code_block = False
            for lineno, line in lines:
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code_block = not in_code_block
                    continue
                if not in_code_block:
                    continue

                if SELECT_STAR_RE.search(line):
                    findings.append({
                        "file": f,
                        "line": lineno,
                        "level": "WARN",
                        "pattern": "SELECT *",
                        "text": stripped,
                    })
                if DESC_IN_SELECT_RE.search(line):
                    findings.append({
                        "file": f,
                        "line": lineno,
                        "level": "INFO",
                        "pattern": "description in SELECT",
                        "text": stripped,
                    })
                if TUSK_SETUP_RE.search(line):
                    findings.append({
                        "file": f,
                        "line": lineno,
                        "level": "INFO",
                        "pattern": "tusk setup",
                        "text": stripped,
                    })
                if HEADER_COLUMN_RE.search(line):
                    findings.append({
                        "file": f,
                        "line": lineno,
                        "level": "INFO",
                        "pattern": "-header -column query",
                        "text": stripped,
                    })

        if findings:
            results.append({"skill": name, "findings": findings})
    return results


# ── Category 4: Redundancy Detection ────────────────────────────────

TUSK_CMD_RE = re.compile(r"\btusk\s+([\w-]+)")


def analyze_redundancy(skills):
    """Find duplicate tusk command invocations and setup + re-fetch patterns."""
    results = []
    for name, dirpath, source in skills:
        files = list_files_in_dir(dirpath)
        tusk_commands = []  # (cmd, file, lineno)

        for f in files:
            lines = read_lines(os.path.join(dirpath, f))
            in_code_block = False
            for lineno, line in lines:
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code_block = not in_code_block
                    continue
                if not in_code_block:
                    continue
                m = TUSK_CMD_RE.search(line)
                if m:
                    tusk_commands.append((m.group(1), f, lineno))

        # Find duplicate commands (same full invocation line)
        duplicates = []
        cmd_counts = {}
        for cmd, f, lineno in tusk_commands:
            cmd_counts.setdefault(cmd, []).append((f, lineno))
        for cmd, locs in cmd_counts.items():
            if len(locs) > 1 and cmd not in ("commit", "criteria", "progress"):
                # Exclude commands that are legitimately called multiple times
                duplicates.append({
                    "command": f"tusk {cmd}",
                    "count": len(locs),
                    "locations": [f"{f}:{ln}" for f, ln in locs],
                })

        # Check for setup + component re-fetch
        has_setup = any(cmd == "setup" for cmd, _, _ in tusk_commands)
        refetch_cmds = {"config", "conventions"}
        refetches = []
        if has_setup:
            for cmd, f, lineno in tusk_commands:
                if cmd in refetch_cmds:
                    refetches.append({
                        "command": f"tusk {cmd}",
                        "file": f,
                        "line": lineno,
                        "note": "redundant — already included in tusk setup output",
                    })

        if duplicates or refetches:
            results.append({
                "skill": name,
                "duplicates": duplicates,
                "refetches": refetches,
            })
    return results


# ── Category 5: Narrative Density ───────────────────────────────────


def analyze_narrative_density(skills):
    """Measure prose-to-code-block ratio per skill, flag ratio > 3.0."""
    results = []
    for name, dirpath, source in skills:
        skill_md = os.path.join(dirpath, "SKILL.md")
        lines = read_lines(skill_md)
        if not lines:
            continue

        prose_lines = 0
        code_lines = 0
        in_code_block = False

        for _, line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                code_lines += 1
            elif stripped:  # non-empty, non-code
                prose_lines += 1

        ratio = prose_lines / max(code_lines, 1)
        flagged = ratio > 3.0

        results.append({
            "name": name,
            "prose_lines": prose_lines,
            "code_lines": code_lines,
            "ratio": round(ratio, 1),
            "flagged": flagged,
        })
    results.sort(key=lambda x: x["ratio"], reverse=True)
    return results


# ── Output formatters ───────────────────────────────────────────────


def format_full_report(census, companions, sql_patterns, redundancy, density):
    """Human-readable full report."""
    out = []
    out.append("=" * 60)
    out.append("Token Audit Report")
    out.append("=" * 60)

    # Summary stats
    total_skills = len(census)
    total_lines = sum(s["total_lines"] for s in census)
    total_tokens = sum(s["estimated_tokens"] for s in census)
    out.append(f"\nSkills scanned: {total_skills}")
    out.append(f"Total lines: {total_lines:,}")
    out.append(f"Estimated tokens: {total_tokens:,}")
    out.append("")

    # Category 1: Size Census
    out.append("-" * 60)
    out.append("1. Skill Size Census (ranked by total context cost)")
    out.append("-" * 60)
    out.append(f"{'Skill':<25} {'SKILL.md':>10} {'Companion':>10} {'Total':>10} {'~Tokens':>10}")
    out.append(f"{'─' * 25} {'─' * 10} {'─' * 10} {'─' * 10} {'─' * 10}")
    for s in census:
        out.append(
            f"{s['name']:<25} {s['skill_md_lines']:>10} "
            f"{s['companion_lines']:>10} {s['total_lines']:>10} "
            f"{s['estimated_tokens']:>10}"
        )
        for cname, clines in s["companions"]:
            out.append(f"  └─ {cname:<21} {clines:>10}")
    out.append("")

    # Category 2: Companion File Analysis
    out.append("-" * 60)
    out.append("2. Companion File Analysis")
    out.append("-" * 60)
    if companions:
        for entry in companions:
            out.append(f"\n  {entry['skill']}:")
            for d in entry["directives"]:
                cond = "conditional" if d["conditional"] else "UNCONDITIONAL"
                size = f" ({d['target_lines']} lines, ~{d['target_tokens']} tokens)" if d["target_lines"] else ""
                out.append(f"    {d['source_file']}:{d['line']} → {d['target']}")
                out.append(f"      Loading: {cond}{size}")
    else:
        out.append("  No Read file: directives found.")
    out.append("")

    # Category 3: SQL Anti-Patterns
    out.append("-" * 60)
    out.append("3. SQL Anti-Patterns")
    out.append("-" * 60)
    if sql_patterns:
        for entry in sql_patterns:
            out.append(f"\n  {entry['skill']}:")
            for f in entry["findings"]:
                out.append(f"    [{f['level']}] {f['file']}:{f['line']} — {f['pattern']}")
                out.append(f"      {f['text']}")
    else:
        out.append("  No SQL anti-patterns found.")
    out.append("")

    # Category 4: Redundancy Detection
    out.append("-" * 60)
    out.append("4. Redundancy Detection")
    out.append("-" * 60)
    if redundancy:
        for entry in redundancy:
            out.append(f"\n  {entry['skill']}:")
            for d in entry["duplicates"]:
                locs = ", ".join(d["locations"])
                out.append(f"    Duplicate: {d['command']} x{d['count']} — {locs}")
            for r in entry["refetches"]:
                out.append(f"    Refetch: {r['command']} at {r['file']}:{r['line']} — {r['note']}")
    else:
        out.append("  No redundancies detected.")
    out.append("")

    # Category 5: Narrative Density
    out.append("-" * 60)
    out.append("5. Narrative Density (prose:code ratio in SKILL.md)")
    out.append("-" * 60)
    out.append(f"{'Skill':<25} {'Prose':>8} {'Code':>8} {'Ratio':>8} {'Flag':>6}")
    out.append(f"{'─' * 25} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 6}")
    for d in density:
        flag = ">>>" if d["flagged"] else ""
        out.append(
            f"{d['name']:<25} {d['prose_lines']:>8} {d['code_lines']:>8} "
            f"{d['ratio']:>8.1f} {flag:>6}"
        )
    flagged = [d for d in density if d["flagged"]]
    if flagged:
        out.append(f"\n  {len(flagged)} skill(s) exceed 3.0 prose:code ratio")
    out.append("")

    return "\n".join(out)


def format_summary(census, companions, sql_patterns, redundancy, density):
    """Top-level stats + top 5 offenders."""
    out = []
    total_skills = len(census)
    total_lines = sum(s["total_lines"] for s in census)
    total_tokens = sum(s["estimated_tokens"] for s in census)
    total_sql = sum(len(e["findings"]) for e in sql_patterns)
    total_redundancies = sum(
        len(e["duplicates"]) + len(e["refetches"]) for e in redundancy
    )
    flagged_density = sum(1 for d in density if d["flagged"])
    unconditional = sum(
        1 for e in companions for d in e["directives"] if not d["conditional"]
    )

    out.append("Token Audit Summary")
    out.append(f"  Skills: {total_skills}")
    out.append(f"  Total lines: {total_lines:,}")
    out.append(f"  Estimated tokens: {total_tokens:,}")
    out.append(f"  SQL anti-patterns: {total_sql}")
    out.append(f"  Redundancies: {total_redundancies}")
    out.append(f"  Unconditional companion loads: {unconditional}")
    out.append(f"  High narrative density: {flagged_density}")
    out.append("")
    out.append("Top 5 by context cost:")
    for s in census[:5]:
        companions_str = ""
        if s["companions"]:
            names = ", ".join(f"{c[0]}({c[1]})" for c in s["companions"])
            companions_str = f"  [{names}]"
        out.append(
            f"  {s['name']:<25} {s['total_lines']:>5} lines  ~{s['estimated_tokens']:>5} tokens{companions_str}"
        )

    return "\n".join(out)


def build_json(census, companions, sql_patterns, redundancy, density):
    """Machine-readable JSON output."""
    return {
        "summary": {
            "skills_scanned": len(census),
            "total_lines": sum(s["total_lines"] for s in census),
            "estimated_tokens": sum(s["estimated_tokens"] for s in census),
            "sql_antipatterns": sum(len(e["findings"]) for e in sql_patterns),
            "redundancies": sum(
                len(e["duplicates"]) + len(e["refetches"]) for e in redundancy
            ),
            "unconditional_loads": sum(
                1 for e in companions for d in e["directives"] if not d["conditional"]
            ),
            "high_narrative_density": sum(1 for d in density if d["flagged"]),
        },
        "size_census": census,
        "companion_analysis": companions,
        "sql_antipatterns": sql_patterns,
        "redundancy": redundancy,
        "narrative_density": density,
    }


# ── Main ─────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print("Usage: tusk-token-audit.py <repo_root> [--summary | --json]", file=sys.stderr)
        sys.exit(2)

    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(2)

    mode = "full"
    for arg in sys.argv[2:]:
        if arg == "--summary":
            mode = "summary"
        elif arg == "--json":
            mode = "json"
        else:
            print(f"Unknown flag: {arg}", file=sys.stderr)
            print("Usage: tusk-token-audit.py <repo_root> [--summary | --json]", file=sys.stderr)
            sys.exit(2)

    skills = list(find_skill_dirs(root))
    if not skills:
        print("No skills found.", file=sys.stderr)
        sys.exit(1)

    census = analyze_size_census(skills)
    companions = analyze_companions(skills)
    sql_patterns = analyze_sql_antipatterns(skills)
    redundancy = analyze_redundancy(skills)
    density = analyze_narrative_density(skills)

    if mode == "json":
        print(json.dumps(build_json(census, companions, sql_patterns, redundancy, density), indent=2))
    elif mode == "summary":
        print(format_summary(census, companions, sql_patterns, redundancy, density))
    else:
        print(format_full_report(census, companions, sql_patterns, redundancy, density))


if __name__ == "__main__":
    main()
