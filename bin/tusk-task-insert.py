#!/usr/bin/env python3
"""Insert a new task with optional criteria in one atomic operation.

Called by the tusk wrapper:
    tusk task-insert "<summary>" "<description>" [flags...]
    tusk task-insert "<summary>" --description-file /path/to/body [flags...]

Arguments received from tusk:
    sys.argv[1] — DB path
    sys.argv[2] — config path
    sys.argv[3:] — summary, description or --description-file, and optional flags

Run 'tusk task-insert --help' for the full flag reference.

Internally validates all enum values against config, runs duplicate
detection, and inserts the task + criteria in one transaction.

Exit codes:
    0 — success (prints JSON with task_id)
    1 — duplicate found (prints JSON with duplicate info)
    2 — validation or database error
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import posixpath
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tusk_loader  # loads tusk-db-lib.py, tusk-git-helpers.py, tusk-json-lib.py, tusk-criteria.py

TUSK_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tusk")

_db_lib = tusk_loader.load("tusk-db-lib")
_git_helpers = tusk_loader.load("tusk-git-helpers")
_json_lib = tusk_loader.load("tusk-json-lib")
dumps = _json_lib.dumps
get_connection = _db_lib.get_connection
load_config = _db_lib.load_config
validate_enum = _db_lib.validate_enum
extract_paths = _git_helpers.extract_paths
extract_referenced_basenames = _git_helpers.extract_referenced_basenames
is_prose_identifier_path = _git_helpers.is_prose_identifier_path
path_exists_in_repo = _git_helpers.path_exists_in_repo
warn_file_spec_glob_metachars = _git_helpers.warn_file_spec_glob_metachars


_RELATIVE_NOT_BEFORE_RE = re.compile(r"^\+(\d+)([mhdw])$")
_GLOB_METACHARS = set("*?[")
_BARE_FILENAME_RE = r"[A-Za-z0-9][\w.-]*\.[A-Za-z0-9][\w.-]*"
_SIBLING_ITEM_RE = re.compile(
    rf"(?:\s*(?:,|\band\b|\bor\b)\s*|\s+/\s*|/\s+)"
    rf"(?P<item>\{{[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)+\}}\.[A-Za-z0-9][\w.-]*|"
    rf"{_BARE_FILENAME_RE}(?:/{_BARE_FILENAME_RE})*)"
)
_BRACED_PATH_RE = re.compile(
    # "@" in the dir segment keeps the full prefix of brace lists under a
    # literal @ directory (apps/web/@/components/ui/{a,b}.tsx, issue #1047).
    r"(?P<dir>(?:[\w.@_-]+/)+)"
    r"\{(?P<names>[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)+)\}"
    r"(?P<ext>\.[A-Za-z0-9][\w.-]*)"
)
_DIRECTORY_LIST_RE = re.compile(
    r"(?P<dir>(?:\.?[A-Za-z0-9][\w.-]*/)+)\s*:\s*(?P<tail>[^\n]{1,300})"
)
_ROUTE_SHORTFORM_RE = re.compile(
    # "@" in the lookbehind stops the shortform from re-extracting the tail
    # of an @-segment path that _PATH_RE now captures in full (issue #1047).
    r"(?<![\w:/.@-])/(?P<path>[A-Za-z0-9][\w./\[\]-]*\.[A-Za-z][\w]{1,9})"
)
_TEST_TARGET_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:UI)?Tests)\b")
_TASK_COMMIT_SHA_RE = re.compile(
    r"(?:\[?TASK-[A-Za-z0-9_-]+\]?)\b.{0,120}?\bcommit\s+([0-9a-fA-F]{7,40})\b",
    re.IGNORECASE | re.DOTALL,
)
_NUMBERED_FILE_SET_RE = re.compile(
    r"\b(?:[2-9]\d*)\s+(?:[\w-]+\s+){0,4}"
    r"(?:files?|paths?|scrapers?|venues?|modules?)\b",
    re.IGNORECASE,
)
_OBVIOUS_REPO_PATH_RE = re.compile(
    r'(?:^|[\s\'"`(,])'
    r'((?:apps|app|src|test|tests|bin|docs|doc|skills|skills-internal|hooks)/'
    r'[\w./_-]+)',
    re.MULTILINE,
)


def _format_utc(value: datetime) -> str:
    """Return a SQLite-friendly UTC timestamp."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_not_before(value: str) -> str:
    """Parse --not-before as UTC, accepting ISO datetimes or +Nm/+Nh/+Nd/+Nw."""
    raw = (value or "").strip()
    match = _RELATIVE_NOT_BEFORE_RE.match(raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return _format_utc(datetime.now(timezone.utc) + delta)

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--not-before must be an ISO datetime (for example "
            "2026-06-01T06:00:00Z) or a relative offset like +4h"
        ) from exc
    return _format_utc(parsed)


def _typed_criterion_type(value: str) -> dict:
    """Parse a JSON string into a typed-criteria dict."""
    try:
        tc = json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"--typed-criteria must be valid JSON: {e}")
    if not isinstance(tc, dict) or "text" not in tc:
        raise argparse.ArgumentTypeError('--typed-criteria must have at least a "text" key')
    return tc


def _read_text_file_argument(path: str, *, arg_name: str) -> str:
    """Read a UTF-8 text argument from a file path, or stdin when path is '-'."""
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: could not read {path!r}: {exc.strerror}"
        ) from exc


def run_dupe_check(summary: str, domain: str | None) -> dict | None:
    """Run tusk dupes check and return match info if duplicate found."""
    cmd = [TUSK_BIN, "dupes", "check", summary, "--json"]
    if domain:
        cmd.extend(["--domain", domain])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 1:
        # Duplicate found
        try:
            data = json.loads(result.stdout)
            dupes = data.get("duplicates", [])
            if dupes:
                return dupes[0]  # highest similarity match
        except json.JSONDecodeError:
            pass
        return {"id": "unknown", "similarity": 0}
    return None


def _repo_root(config_path: str, explicit_repo_root: str | None = None) -> str | None:
    if explicit_repo_root:
        return explicit_repo_root
    env_root = os.environ.get("TUSK_REPO_ROOT") or os.environ.get("TUSK_PROJECT")
    if env_root:
        return env_root
    if not config_path:
        return None
    config_dir = os.path.dirname(os.path.abspath(config_path))
    if os.path.basename(config_dir) == "tusk":
        return os.path.dirname(config_dir)
    return config_dir


def _has_glob_metachar(path: str) -> bool:
    return any(ch in path for ch in _GLOB_METACHARS)


def _expand_scope_patterns(patterns: list[str]) -> list[str]:
    expanded = []
    for pattern in patterns:
        for entry in str(pattern or "").split(","):
            entry = entry.strip()
            if entry:
                expanded.append(entry)
    return expanded


def _obvious_spec_paths(spec: str) -> list[str]:
    paths = []
    seen = set()
    for path in extract_paths(spec):
        if path not in seen:
            seen.add(path)
            paths.append(path)
    for match in _OBVIOUS_REPO_PATH_RE.finditer(spec or ""):
        path = match.group(1).strip().rstrip('.,;:\'"`)')
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _warn_missing_path(path: str, source: str) -> None:
    print(
        f"Warning: task-insert {source} path does not exist at repo root: {path}",
        file=sys.stderr,
    )


def _warn_for_missing_declared_paths(
    repo_root: str | None,
    scope_patterns: list[str],
    typed_criteria: list[dict],
) -> None:
    # File-type specs are globbed at verification time, so metacharacters in
    # them are ambiguous regardless of whether a repo root resolved (#1032).
    for tc in typed_criteria:
        if tc.get("type") == "file":
            warn_file_spec_glob_metachars(tc.get("spec"), "task-insert")

    if not repo_root:
        return

    for pattern in scope_patterns:
        if _has_glob_metachar(pattern):
            continue
        if not path_exists_in_repo(repo_root, pattern):
            _warn_missing_path(pattern, "--scope")

    warned_specs: set[str] = set()
    for tc in typed_criteria:
        spec = tc.get("spec") or ""
        for path in _obvious_spec_paths(spec):
            if _has_glob_metachar(path) or path in warned_specs:
                continue
            warned_specs.add(path)
            if not path_exists_in_repo(repo_root, path):
                _warn_missing_path(path, "verification_spec")


def _path_file_portion(path: str) -> str:
    """Return the file path portion of a pytest nodeid-like token."""
    return (path or "").split("::", 1)[0].strip()


def _tracked_repo_files(repo_root: str | None) -> list[str]:
    if not repo_root:
        return []
    result = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split("\0") if p]


def _resolve_auto_derived_scope_pattern(repo_root: str | None, pattern: str) -> str:
    """Resolve a non-existing auto-derived path by unique repo suffix.

    Exact paths win. Missing paths are fuzzy-matched only when exactly one
    tracked file ends with the extracted pattern, so ambiguous prose keeps the
    literal value for the operator to expand later.
    """
    raw = (pattern or "").strip()
    file_part = _path_file_portion(raw)
    if not file_part or _has_glob_metachar(file_part):
        return raw
    if raw.endswith("/"):
        return f"{file_part.rstrip('/')}/**"
    if path_exists_in_repo(repo_root, file_part):
        return file_part

    matches = [
        path for path in _tracked_repo_files(repo_root)
        if path.endswith(f"/{file_part}")
    ]
    if len(matches) == 1:
        return matches[0]
    return raw


def _resolve_unique_repo_basename(repo_root: str | None, name: str) -> str | None:
    """Resolve ``name`` to a unique tracked repo path by basename."""
    raw = (name or "").strip().strip('.,;:\'"`)')
    if not raw or "/" in raw or _has_glob_metachar(raw):
        return None
    matches = [
        path for path in _tracked_repo_files(repo_root)
        if posixpath.basename(path) == raw
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _test_target_scope_paths(repo_root: str | None, text: str) -> list[str]:
    """Infer tracked files from target-shaped test identifiers.

    iOS task reports often mention target or test-class shapes such as
    ``LaughTrackTests`` or ``SoftPushPromptCoordinatorTests`` without naming
    concrete files. Keep this intentionally narrow: only capitalized tokens
    ending in ``Tests``/``UITests`` participate, and only tracked files whose
    path component or filename stem exactly matches the token are returned.
    """
    if not repo_root or not text:
        return []
    tokens = []
    seen_tokens: set[str] = set()
    for match in _TEST_TARGET_TOKEN_RE.finditer(text):
        token = match.group(1)
        if token not in seen_tokens:
            seen_tokens.add(token)
            tokens.append(token)
    if not tokens:
        return []

    candidates: list[str] = []
    seen_paths: set[str] = set()
    tracked = _tracked_repo_files(repo_root)
    for token in tokens:
        for path in tracked:
            parts = path.split("/")
            stem, _ext = posixpath.splitext(posixpath.basename(path))
            if token not in parts and stem != token:
                continue
            if path not in seen_paths:
                seen_paths.add(path)
                candidates.append(path)
    return candidates


def _commit_referenced_scope_paths(repo_root: str | None, text: str) -> list[str]:
    """Infer scope from a referenced TASK commit and numbered file set.

    Fleet-wide refactor follow-ups often cite a predecessor commit plus prose
    such as "42 venue scrapers" while only enumerating a few examples. Treat
    that pairing as a signal to hydrate the full changed-file list from git.
    """
    if not repo_root or not text or not _NUMBERED_FILE_SET_RE.search(text):
        return []

    candidates: list[str] = []
    seen_paths: set[str] = set()
    seen_shas: set[str] = set()
    for match in _TASK_COMMIT_SHA_RE.finditer(text):
        sha = match.group(1)
        normalized_sha = sha.lower()
        if normalized_sha in seen_shas:
            continue
        seen_shas.add(normalized_sha)
        result = subprocess.run(
            ["git", "-C", repo_root, "show", "--format=", "--name-only", sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            continue
        for path in result.stdout.splitlines():
            path = path.strip()
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            candidates.append(path)
    return candidates


def _expand_sibling_shortform_item(base_dir: str, item: str) -> list[str]:
    """Expand one sibling shortform token under ``base_dir``."""
    raw = (item or "").strip().strip('.,;:\'"`)')
    if not raw:
        return []
    brace = re.fullmatch(
        r"\{(?P<names>[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)+)\}"
        r"(?P<ext>\.[A-Za-z0-9][\w.-]*)",
        raw,
    )
    if brace:
        ext = brace.group("ext")
        return [
            posixpath.normpath(f"{base_dir}/{name.strip()}{ext}")
            for name in brace.group("names").split(",")
            if name.strip()
        ]
    if "/" in raw:
        parts = raw.split("/")
        if all(
            part and "/" not in part and re.fullmatch(_BARE_FILENAME_RE, part)
            for part in parts
        ):
            return [posixpath.normpath(f"{base_dir}/{part}") for part in parts]
        return []
    if re.fullmatch(_BARE_FILENAME_RE, raw):
        return [posixpath.normpath(f"{base_dir}/{raw}")]
    return []


def _sibling_shortform_scope_paths(text: str, extracted_paths: list[str]) -> list[str]:
    """Infer nearby sibling filenames from an explicit path's directory.

    This covers prose such as ``dir/Alpha.swift and Beta.swift`` and
    ``dir/Alpha.swift and {Beta,Gamma}.swift`` without changing the generic
    extractor's broader path-token contract.
    """
    if not text:
        return []

    candidates: list[str] = []
    for match in _BRACED_PATH_RE.finditer(text):
        base_dir = match.group("dir").rstrip("/")
        ext = match.group("ext")
        for name in match.group("names").split(","):
            name = name.strip()
            if name:
                candidates.append(posixpath.normpath(f"{base_dir}/{name}{ext}"))

    for base_path in extracted_paths:
        base = _path_file_portion(base_path)
        if "/" not in base or _has_glob_metachar(base):
            continue
        base_dir = posixpath.dirname(base)
        if not base_dir:
            continue
        search_start = 0
        while True:
            index = text.find(base_path, search_start)
            if index == -1:
                break
            tail = text[index + len(base_path):]
            tail = tail.split("\n", 1)[0][:200]
            for item_match in _SIBLING_ITEM_RE.finditer(tail):
                candidates.extend(
                    _expand_sibling_shortform_item(base_dir, item_match.group("item"))
                )
            search_start = index + len(base_path)

    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _directory_list_scope_paths(text: str) -> list[str]:
    """Infer comma-separated bare filenames after an explicit directory."""
    if not text:
        return []

    candidates: list[str] = []
    for match in _DIRECTORY_LIST_RE.finditer(text):
        base_dir = match.group("dir").rstrip("/")
        tail = match.group("tail")
        for item_match in re.finditer(_BARE_FILENAME_RE, tail):
            item = item_match.group(0).strip().rstrip('.,;:\'"`)')
            if not item:
                continue
            candidates.append(
                posixpath.normpath(f"{base_dir}/{item}")
            )

    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _route_shortform_scope_paths(repo_root: str | None, text: str) -> list[str]:
    """Resolve route-only path mentions by unique tracked suffix."""
    if not text:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for match in _ROUTE_SHORTFORM_RE.finditer(text):
        raw = match.group("path").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        resolved = _resolve_auto_derived_scope_pattern(repo_root, raw)
        candidates.append(resolved)
    return candidates


_LOCKFILE_SIBLINGS = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")


def _lockfile_sibling_scope_paths(text: str, candidates: list[str]) -> list[str]:
    """Pair package.json scope rows with their named sibling lockfiles (issue #1052).

    A package.json edit and its lockfile regeneration always travel together,
    so deriving one without the other guarantees either a commit-time guard
    rejection or a hand-added expansion. Bare lockfile mentions rarely resolve
    on their own — most repos contain several (root + per-app), so the
    unique-basename resolver skips them; the already-derived package.json row
    supplies the directory instead. Only lockfiles actually named in the text
    are paired, so ordinary package.json edits gain no scope noise.
    """
    if not text:
        return []
    lowered = text.lower()
    named = [name for name in _LOCKFILE_SIBLINGS if name in lowered]
    if not named:
        return []
    siblings: list[str] = []
    for cand in candidates:
        if posixpath.basename(cand) != "package.json":
            continue
        directory = posixpath.dirname(cand)
        for name in named:
            siblings.append(posixpath.join(directory, name) if directory else name)
    return siblings


# Counterfactual path mentions (issue #1071). A path immediately followed by a
# negation phrase — "(does not exist)", "(deleted)", "(removed in TASK-12)" —
# or preceded by not/never inside the same clause describes a path the task
# will NOT touch, so deriving it produces a misleading scope row. The windows
# are deliberately narrow: trailing match must start right after the token
# (optionally across "," / ":" / dashes and one open paren), and the leading
# clause is cut at sentence/clause boundaries including parentheses, so
# "(does not exist; real path is app/globals.css)" never poisons the real
# path after the semicolon.
_NEGATION_TRAILING_RE = re.compile(
    r"^[\s,:—–-]*\(?\s*(?:which\s+|it\s+)?(?:was\s+|is\s+|are\s+|were\s+)?"
    r"(?:does\s*n(?:o|')t\s+exist|did\s+not\s+exist|no\s+longer\s+exists?|"
    r"never\s+existed|nonexistent\b|deleted\b|removed\b)",
    re.IGNORECASE,
)
_NEGATION_CLAUSE_BOUNDARY_RE = re.compile(r"[.;:!?\n()]")
_NEGATION_LEADING_RE = re.compile(r"\b(?:not|never)\b", re.IGNORECASE)
_NEGATION_LEADING_WINDOW = 60


def _is_negated_mention(text: str, start: int, end: int) -> bool:
    """True when the text span at [start, end) sits in a negation window."""
    if _NEGATION_TRAILING_RE.search(text[end:end + 60]):
        return True
    window = text[max(0, start - _NEGATION_LEADING_WINDOW):start]
    clause = _NEGATION_CLAUSE_BOUNDARY_RE.split(window)[-1]
    return bool(_NEGATION_LEADING_RE.search(clause))


def _negated_path_mentions(text: str, candidates: list[str]) -> set[str]:
    """Return the candidates whose every literal mention in ``text`` is negated.

    Candidates that never appear literally (e.g. a bare basename resolved to a
    repo path) fall back to their basename token. A candidate with at least
    one non-negated mention is kept — only unanimously counterfactual
    mentions drop.
    """
    if not text:
        return set()
    negated: set[str] = set()
    for cand in dict.fromkeys(candidates):
        token = cand
        if token not in text:
            token = posixpath.basename(_path_file_portion(cand) or "")
            if not token or token not in text:
                continue
        found = False
        all_negated = True
        search_start = 0
        while True:
            index = text.find(token, search_start)
            if index == -1:
                break
            found = True
            if not _is_negated_mention(text, index, index + len(token)):
                all_negated = False
                break
            search_start = index + len(token)
        if found and all_negated:
            negated.add(cand)
    return negated


# Explicit unit-test requirement in a criterion or description (issue #1073).
# Deliberately narrow: bare "tests pass" prose must not trigger sibling
# derivation, only an explicit unit-test mention does.
_UNIT_TEST_REQUIREMENT_RE = re.compile(r"\bunit[\s_-]?tests?\b", re.IGNORECASE)

# Source extensions whose sibling test convention is <stem>.test.<ext> /
# <stem>.spec.<ext> in the same directory.
_TEST_SIBLING_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _test_sibling_scope_paths(repo_root: str | None, candidates: list[str]) -> list[str]:
    """Pair derived source rows with their on-disk sibling test files (issue #1073).

    When a criterion explicitly requires unit tests, the conventional sibling
    test file travels with the source edit the same way a lockfile travels
    with package.json (issue #1052) — deriving one without the other forces a
    mid-task scope expansion. Only siblings that actually exist on disk are
    derived, so speculative conventions add no scope noise; the caller gates
    on the unit-test requirement across all of the task's text blocks.
    """
    if not repo_root or not candidates:
        return []
    siblings: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        base = _path_file_portion(cand)
        if not base or _has_glob_metachar(base):
            continue
        directory = posixpath.dirname(base)
        stem, ext = posixpath.splitext(posixpath.basename(base))
        if not stem or not ext:
            continue
        lowered_stem = stem.lower()
        names: list[str] = []
        if ext.lower() in _TEST_SIBLING_JS_EXTS:
            if lowered_stem.endswith((".test", ".spec")):
                continue
            names = [f"{stem}.test{ext}", f"{stem}.spec{ext}"]
        elif ext.lower() == ".py":
            if lowered_stem.startswith("test_") or lowered_stem.endswith("_test"):
                continue
            names = [f"test_{stem}.py", f"{stem}_test.py"]
        elif ext.lower() == ".swift":
            if stem.endswith("Tests"):
                continue
            names = [f"{stem}Tests.swift"]
        for name in names:
            rel = posixpath.join(directory, name) if directory else name
            if rel in seen or rel in candidates:
                continue
            if os.path.isfile(os.path.join(repo_root, rel)):
                seen.add(rel)
                siblings.append(rel)
    return siblings


# Likely code-symbol tokens (issue #1080). SCREAMING_SNAKE requires at least
# one underscore segment so plain acronyms ("JSON", "TASK") never match;
# camelCase must start lowercase with an internal capital so prose-cased
# words ("GitHub", "JavaScript") never match. Length floor filters short
# prose-ish tokens like "macOS".
_SCREAMING_SNAKE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_CAMEL_CASE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:[A-Z][a-zA-Z0-9]*)+\b")
_SYMBOL_MIN_LEN = 6
_SYMBOL_SCAN_CAP = 8


def _extract_symbol_tokens(text: str) -> list[str]:
    """Likely code symbols mentioned in ``text``, capped and deduped."""
    tokens: list[str] = []
    seen: set[str] = set()
    for rx in (_SCREAMING_SNAKE_RE, _CAMEL_CASE_RE):
        for m in rx.finditer(text or ""):
            tok = m.group(0)
            if len(tok) < _SYMBOL_MIN_LEN or tok in seen:
                continue
            seen.add(tok)
            tokens.append(tok)
            if len(tokens) >= _SYMBOL_SCAN_CAP:
                return tokens
    return tokens


def _symbol_definition_scope_paths(
    repo_root: str | None, text: str, known_paths: list[str]
) -> list[str]:
    """Resolve bare code-symbol mentions to their unique definition files
    (issue #1080).

    Tasks routinely describe scope via symbols ("extend
    LINEUP_COMEDIAN_SELECT and mapLineupItem ...") without naming the files
    that define them; the path extractor cannot see those. For each likely
    symbol token, ``git grep`` the repo for definition-shaped sites
    (export/const/function/class/interface/type/enum in JS/TS, def/class in
    Python, module-level assignment for constants). Exactly one matching
    file -> auto-derived candidate; zero or multiple matches are skipped
    silently so ambiguous symbols never add noise rows. Best-effort: any
    git failure skips that symbol. Symbol tokens are guaranteed
    ``[A-Za-z0-9_]+`` by the extraction regexes, so they embed safely in
    the grep pattern without escaping.
    """
    if not repo_root or not text:
        return []
    symbols = _extract_symbol_tokens(text)
    if not symbols:
        return []
    known = set(known_paths or [])
    out: list[str] = []
    seen: set[str] = set()
    # Word-boundary tail spelled as a character class — git grep's ERE
    # engine on macOS does not reliably support \b.
    tail = "([^A-Za-z0-9_]|$)"
    for sym in symbols:
        pattern = (
            "((export[[:space:]]+)?"
            "(const|let|var|function|class|interface|type|enum)"
            f"[[:space:]]+{sym}{tail})"
            f"|(def[[:space:]]+{sym}{tail})"
            f"|(^{sym}[[:space:]]*=)"
        )
        result = subprocess.run(
            ["git", "-C", repo_root, "grep", "-lE", pattern],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            # 1 = no definition site found; anything else = git error.
            # Both skip silently — this derivation is best-effort.
            continue
        files = [f for f in result.stdout.splitlines() if f.strip()]
        if len(files) != 1:
            continue
        path = files[0]
        if path not in seen and path not in known:
            seen.add(path)
            out.append(path)
    return out


def _auto_scope_candidates(
    text: str,
    *,
    repo_root: str | None = None,
    task_type: str | None = None,
    requires_unit_tests: bool | None = None,
) -> list[str]:
    """Return explicit and inferred auto-scope candidates for one text block.

    ``requires_unit_tests`` gates the sibling-test derivation (issue #1073).
    Pass the any-block answer when iterating multiple text blocks — the
    unit-test requirement usually lives in a criterion while the source path
    lives in the description. ``None`` (default) detects from this block only.
    """
    explicit = extract_paths(text)
    target_paths = _test_target_scope_paths(repo_root, text)
    if target_paths and (task_type or "").lower() != "docs":
        explicit = [
            p for p in explicit
            if not p.lower().endswith((".md", ".markdown"))
        ]
    bare_paths = [
        resolved for name in extract_referenced_basenames(text)
        if (resolved := _resolve_unique_repo_basename(repo_root, name))
    ]
    candidates = [
        *explicit,
        *_sibling_shortform_scope_paths(text, explicit),
        *_directory_list_scope_paths(text),
        *_route_shortform_scope_paths(repo_root, text),
        *bare_paths,
        *target_paths,
        *_commit_referenced_scope_paths(repo_root, text),
    ]
    # Symbol-to-definition resolution (issue #1080) runs after the
    # path-shaped derivations so its known-paths filter can skip files
    # already covered, and before the sibling-test pass so resolved
    # definition files pick up their test siblings too.
    candidates += _symbol_definition_scope_paths(repo_root, text, candidates)
    if requires_unit_tests is None:
        requires_unit_tests = bool(_UNIT_TEST_REQUIREMENT_RE.search(text or ""))
    test_siblings = (
        _test_sibling_scope_paths(repo_root, candidates)
        if requires_unit_tests
        else []
    )
    combined = [
        *candidates,
        *_lockfile_sibling_scope_paths(text, candidates),
        *test_siblings,
    ]
    negated = _negated_path_mentions(text, combined)
    if negated:
        combined = [c for c in combined if c not in negated]
    return combined


def _warn_already_passing_criteria(typed_inserted: list) -> None:
    """Warn when an inserted code/file criterion spec already passes (issue #1061).

    Insert-time convergence signal mirroring task-start's
    criteria_already_passing count (TASK-624 / issue #1051): runs the same
    timeout-bounded run_verification runner, excludes test-type specs for
    latency, and is best-effort by design — any failure degrades to silence
    and never changes task-insert's exit code or JSON stdout.
    """
    try:
        candidates = [
            (cid, ctype, spec)
            for cid, ctype, spec in typed_inserted
            if ctype in ("code", "file") and spec
        ]
        if not candidates:
            return
        run_verification = tusk_loader.load("tusk-criteria").run_verification
        for cid, ctype, spec in candidates:
            if run_verification(ctype, spec)["passed"]:
                print(
                    f"Warning: criterion #{cid}'s verification spec already "
                    f"passes ({ctype}: {spec}) — the work may already be done; "
                    "check for convergent completion before starting this task.",
                    file=sys.stderr,
                )
    except Exception:
        pass


def _canonical_enum_value(value: str, valid_values: list[str]) -> str:
    """Return the configured enum spelling for a case-insensitive match."""
    if not valid_values:
        return value
    if value in valid_values:
        return value
    lowered = value.lower()
    for valid in valid_values:
        if valid.lower() == lowered:
            return valid
    return value


def main(argv: list[str]) -> int:
    db_path = argv[0]
    config_path = argv[1]
    parser = argparse.ArgumentParser(allow_abbrev=False,
        prog="tusk task-insert",
        description="Insert a new task with criteria in one atomic operation",
    )
    parser.add_argument("summary", help="Task summary")
    parser.add_argument("description", nargs="?", help="Task description")
    parser.add_argument("--description", dest="description_flag",
                        help="Task description (alias for the second positional argument)")
    parser.add_argument("--description-file", dest="description_file",
                        help="Read task description from a UTF-8 file, or '-' for stdin")
    parser.add_argument("--priority", default="Medium", help="Priority (default: Medium)")
    parser.add_argument("--domain", default=None, help="Domain")
    parser.add_argument("--task-type", default="feature", dest="task_type", help="Task type (default: feature)")
    parser.add_argument("--assignee", default=None, help="Assignee")
    parser.add_argument("--complexity", default="M", help="Complexity (default: M)")
    parser.add_argument("--criteria", action="append", default=[], metavar="TEXT",
                        help="Acceptance criterion text (repeatable)")
    parser.add_argument("--typed-criteria", action="append", default=[], type=_typed_criterion_type,
                        dest="typed_criteria", metavar="JSON",
                        help='Typed criterion as JSON, e.g. \'{"text":"...","type":"...","spec":"..."}\' (repeatable)')
    parser.add_argument("--repo-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--workflow", default=None, help="Workflow (validated against config)")
    parser.add_argument("--expires-in", type=int, default=None, dest="expires_in_days", metavar="DAYS",
                        help="Set expires_at to +N days")
    parser.add_argument("--not-before", type=_parse_not_before, default=None, dest="not_before",
                        metavar="TIMESTAMP",
                        help="Do not surface/start the task before this UTC time; accepts ISO or +Nm/+Nh/+Nd/+Nw")
    parser.add_argument("--fixes-task-id", type=int, default=None, dest="fixes_task_id", metavar="ID",
                        help="Link this task as a follow-up/rework of the given task id")
    parser.add_argument("--scope", action="append", default=[], metavar="PATTERN",
                        help="Declare an in-scope path (source='operator_declared'). Repeatable.")
    parser.add_argument("--creates", action="append", default=[], metavar="PATH",
                        help="Declare a path the task will create (source='creates'). Repeatable.")
    parser.add_argument("--unbounded", action="store_true", default=False,
                        help="Mark this task as legitimately spanning the repo — emits an 'unbounded' "
                             "scope row that signals the commit-time scope guard to silently pass.")
    args = parser.parse_args(argv[2:])

    summary = args.summary
    description_sources = [
        args.description is not None,
        args.description_flag is not None,
        args.description_file is not None,
    ]
    if sum(description_sources) > 1:
        parser.error(
            "description was provided multiple ways; use only one of positional "
            "description, --description, or --description-file"
        )
    if args.description_file is not None:
        description = _read_text_file_argument(
            args.description_file, arg_name="--description-file"
        )
    else:
        description = args.description if args.description is not None else args.description_flag
    if description is None:
        parser.error(
            "description is required; pass it as the second positional argument, "
            "with --description, or with --description-file"
        )
    priority = args.priority
    domain = args.domain
    task_type = args.task_type
    assignee = args.assignee
    complexity = args.complexity
    workflow = args.workflow
    criteria: list[str] = args.criteria
    typed_criteria: list[dict] = args.typed_criteria
    expires_in_days = args.expires_in_days
    not_before = args.not_before
    fixes_task_id = args.fixes_task_id
    scope_patterns: list[str] = _expand_scope_patterns(args.scope)
    creates_paths: list[str] = args.creates
    unbounded: bool = args.unbounded

    if not criteria and not typed_criteria:
        parser.error(
            "at least one acceptance criterion is required. "
            "Use --criteria \"...\" or --typed-criteria '{\"text\":\"...\"}' to add one."
        )

    # Load and validate against config
    config = load_config(config_path)
    priority = _canonical_enum_value(priority, config.get("priorities", []))

    errors = []
    err = validate_enum(priority, config.get("priorities", []), "priority")
    if err:
        errors.append(err)
    err = validate_enum(task_type, config.get("task_types", []), "task_type")
    if err:
        errors.append(err)
    err = validate_enum(complexity, config.get("complexity", []), "complexity")
    if err:
        errors.append(err)

    if domain is not None:
        err = validate_enum(domain, config.get("domains", []), "domain")
        if err:
            errors.append(err)

    agents = config.get("agents", {})
    if assignee is not None and agents:
        valid_agents = list(agents.keys())
        err = validate_enum(assignee, valid_agents, "assignee")
        if err:
            errors.append(err)

    if workflow is not None:
        err = validate_enum(workflow, config.get("workflows", []), "workflow")
        if err:
            errors.append(err)

    # Validate typed criteria
    criterion_types = config.get("criterion_types", [])
    spec_required_types = {"code", "test", "file"}
    for i, tc in enumerate(typed_criteria):
        ct = tc.get("type", "manual")
        spec = tc.get("spec")
        if spec is not None and not spec.strip():
            tc["spec"] = None  # blank specs must never reach the DB as '' (issue #1045)
        if criterion_types and ct not in criterion_types:
            joined = ", ".join(criterion_types)
            errors.append(f"--typed-criteria[{i}]: invalid type '{ct}'. Valid: {joined}")
        if ct in spec_required_types and not tc.get("spec"):
            errors.append(f"--typed-criteria[{i}]: --spec required for type '{ct}'")

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        return 2

    repo_root = _repo_root(config_path, args.repo_root)
    _warn_for_missing_declared_paths(repo_root, scope_patterns, typed_criteria)

    # Run duplicate check
    dupe = run_dupe_check(summary, domain)
    if dupe:
        result = {
            "duplicate": True,
            "matched_task_id": dupe.get("id"),
            "matched_summary": dupe.get("summary", ""),
            "similarity": dupe.get("similarity", 0),
        }
        print(dumps(result))
        return 1

    # Compute expires_at
    expires_at_expr = None
    if expires_in_days is not None:
        expires_at_expr = f"+{expires_in_days} days"

    # Insert task + criteria in one transaction
    conn = get_connection(db_path)
    try:
        if fixes_task_id is not None:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (fixes_task_id,)
            ).fetchone()
            if row is None:
                print(
                    f"Error: --fixes-task-id {fixes_task_id} does not reference an existing task",
                    file=sys.stderr,
                )
                return 2

        if expires_at_expr:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, workflow, fixes_task_id, "
                "expires_at, not_before, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, ?, datetime('now', ?), "
                "?, datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, workflow, fixes_task_id, expires_at_expr, not_before),
            )
        else:
            conn.execute(
                "INSERT INTO tasks (summary, description, status, priority, domain, "
                "task_type, assignee, complexity, workflow, fixes_task_id, "
                "not_before, created_at, updated_at) "
                "VALUES (?, ?, 'To Do', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (summary, description, priority, domain, task_type, assignee,
                 complexity, workflow, fixes_task_id, not_before),
            )

        task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        criteria_ids = []
        for criterion in criteria:
            conn.execute(
                "INSERT INTO acceptance_criteria (task_id, criterion, source) "
                "VALUES (?, ?, 'original')",
                (task_id, criterion),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            criteria_ids.append(cid)

        typed_inserted = []
        for tc in typed_criteria:
            conn.execute(
                "INSERT INTO acceptance_criteria "
                "(task_id, criterion, source, criterion_type, verification_spec) "
                "VALUES (?, ?, 'original', ?, ?)",
                (task_id, tc["text"], tc.get("type", "manual"), tc.get("spec")),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            criteria_ids.append(cid)
            typed_inserted.append((cid, tc.get("type", "manual"), tc.get("spec")))

        for pattern in scope_patterns:
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (?, ?, 'operator_declared')",
                (task_id, pattern),
            )
        for path in creates_paths:
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (?, ?, 'creates')",
                (task_id, path),
            )
        if unbounded:
            # Pattern is a sentinel — the scope guard short-circuits when any
            # row for the task has source='unbounded' (emits no patterns,
            # silent pass).
            conn.execute(
                "INSERT INTO task_scope (task_id, pattern, source) "
                "VALUES (?, '**', 'unbounded')",
                (task_id,),
            )
        else:
            # Auto-extract paths from summary/description/criteria/specs.
            # Mirrors the migration-73 backfill (which used
            # task_referenced_paths) so new tasks land with the same
            # task_scope shape that the scope-paths fallback would have
            # inferred from a legacy task. Explicit --scope and --creates
            # rows already inserted above win — auto_derived is only added
            # for paths the operator didn't already declare.
            explicit_patterns = set(scope_patterns) | set(creates_paths)
            text_blocks = [summary or "", description or ""]
            for c in criteria:
                text_blocks.append(c or "")
            for tc in typed_criteria:
                text_blocks.append(tc.get("text") or "")
                text_blocks.append(tc.get("spec") or "")
            seen_auto: set = set()
            requires_unit_tests = any(
                _UNIT_TEST_REQUIREMENT_RE.search(block or "")
                for block in text_blocks
            )
            for text in text_blocks:
                for p in _auto_scope_candidates(
                    text,
                    repo_root=repo_root,
                    task_type=task_type,
                    requires_unit_tests=requires_unit_tests,
                ):
                    resolved = _resolve_auto_derived_scope_pattern(repo_root, p)
                    # ``.github/`` is a real, ubiquitous repo directory, so any
                    # path under it is a genuine reference — never a prose
                    # identifier. The earlier carve-out only matched the
                    # trailing-slash *directory* form (``.github/workflows/``)
                    # and so dropped concrete *file* mentions like
                    # ``.github/workflows/web-ci.yml`` whenever that file did
                    # not yet exist on disk, because is_prose_identifier_path
                    # classifies any non-existent dot-first-segment path as
                    # prose (issue #1084 — co-discovered genuine regression
                    # from the TASK-549 prose filter).
                    is_explicit_github_path = p.startswith(".github/") and p != ".github/"
                    if (
                        not is_explicit_github_path
                        and is_prose_identifier_path(p, repo_root)
                    ):
                        continue
                    if resolved in explicit_patterns or resolved in seen_auto:
                        continue
                    seen_auto.add(resolved)
                    conn.execute(
                        "INSERT INTO task_scope (task_id, pattern, source) "
                        "VALUES (?, ?, 'auto_derived')",
                        (task_id, resolved),
                    )

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"Database error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    _warn_already_passing_criteria(typed_inserted)

    # Run WSJF scoring so the new task gets a priority_score immediately
    subprocess.run([TUSK_BIN, "wsjf"], capture_output=True)

    result = {
        "task_id": task_id,
        "summary": summary,
        "criteria_ids": criteria_ids,
    }
    print(dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].endswith(".db"):
        print("Error: This script must be invoked via the tusk wrapper.", file=sys.stderr)
        print("Use: tusk task-insert \"<summary>\" \"<description>\" [--priority P] [--domain D]", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
