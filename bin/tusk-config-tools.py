#!/usr/bin/env python3
"""Config validation and trigger generation for tusk.

Called by the tusk wrapper:
    tusk validate         → tusk-config-tools.py validate <config_path>
                          → tusk-config-tools.py validate-triggers <config_path> <db_path>
    tusk regen-triggers   → tusk-config-tools.py gen-triggers <config_path>

Arguments:
    sys.argv[1] — subcommand: 'validate', 'gen-triggers', or 'validate-triggers'
    sys.argv[2] — path to the resolved config JSON file
    sys.argv[3] — db_path (validate-triggers only)
"""

import json
import os
import sqlite3
import sys


def cmd_validate(config_path: str) -> int:
    # ── Load JSON ──
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f'Error: {config_path} is not valid JSON.', file=sys.stderr)
        print(f'  {e}', file=sys.stderr)
        return 1

    if not isinstance(cfg, dict):
        print(f'Error: {config_path} must be a JSON object (got {type(cfg).__name__}).', file=sys.stderr)
        return 1

    errors = []

    # ── Check for unknown top-level keys ──
    KNOWN_KEYS = {'domains', 'task_types', 'statuses', 'priorities', 'closed_reasons', 'complexity', 'blocker_types', 'criterion_types', 'workflows', 'agents', 'dupes', 'review', 'review_categories', 'review_severities', 'merge', 'test_command', 'test_command_timeout_sec', 'baseline_min_sample_size', 'domain_test_commands', 'path_test_commands', 'project_type', 'project_libs', 'issue_scoring'}
    known_list = ', '.join(sorted(KNOWN_KEYS))
    unknown = set(cfg.keys()) - KNOWN_KEYS
    if unknown:
        for k in sorted(unknown):
            errors.append(f'Unknown config key "{k}". Valid keys: {known_list}')

    # ── Validate list-of-strings fields ──
    LIST_FIELDS = {
        'domains':           {'required': False},
        'task_types':        {'required': False},
        'statuses':          {'required': True},
        'priorities':        {'required': True},
        'closed_reasons':    {'required': True},
        'complexity':        {'required': False},
        'blocker_types':     {'required': False},
        'criterion_types':   {'required': False},
        'review_categories': {'required': False},
        'review_severities': {'required': False},
        'workflows':         {'required': False},
    }
    for field, opts in LIST_FIELDS.items():
        if field not in cfg:
            if opts['required']:
                errors.append(f'Missing required key "{field}".')
            continue
        val = cfg[field]
        if not isinstance(val, list):
            errors.append(f'"{field}" must be a list (got {type(val).__name__}).')
            continue
        if opts['required'] and len(val) == 0:
            errors.append(f'"{field}" must not be empty.')
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append(f'"{field}[{i}]" must be a string (got {type(item).__name__}: {item!r}).')

    # ── Validate agents (dict of string→string) ──
    if 'agents' in cfg:
        agents = cfg['agents']
        if not isinstance(agents, dict):
            errors.append(f'"agents" must be an object (got {type(agents).__name__}).')
        else:
            for k, v in agents.items():
                if not isinstance(v, str):
                    errors.append(f'"agents.{k}" value must be a string (got {type(v).__name__}: {v!r}).')

    # ── Validate dupes (object with specific sub-keys) ──
    if 'dupes' in cfg:
        dupes = cfg['dupes']
        if not isinstance(dupes, dict):
            errors.append(f'"dupes" must be an object (got {type(dupes).__name__}).')
        else:
            KNOWN_DUPE_KEYS = {'strip_prefixes', 'check_threshold', 'criterion_check_threshold', 'similar_threshold'}
            known_dupe_list = ', '.join(sorted(KNOWN_DUPE_KEYS))
            unknown_dupe = set(dupes.keys()) - KNOWN_DUPE_KEYS
            if unknown_dupe:
                for k in sorted(unknown_dupe):
                    errors.append(f'Unknown key "dupes.{k}". Valid dupes keys: {known_dupe_list}')

            if 'strip_prefixes' in dupes:
                sp = dupes['strip_prefixes']
                if not isinstance(sp, list):
                    errors.append(f'"dupes.strip_prefixes" must be a list (got {type(sp).__name__}).')
                else:
                    for i, item in enumerate(sp):
                        if not isinstance(item, str):
                            errors.append(f'"dupes.strip_prefixes[{i}]" must be a string (got {type(item).__name__}: {item!r}).')

            for thresh in ('check_threshold', 'criterion_check_threshold', 'similar_threshold'):
                if thresh in dupes:
                    tv = dupes[thresh]
                    if not isinstance(tv, (int, float)):
                        errors.append(f'"dupes.{thresh}" must be a number (got {type(tv).__name__}: {tv!r}).')
                    elif not (0 <= tv <= 1):
                        errors.append(f'"dupes.{thresh}" must be between 0 and 1 (got {tv}).')

    # ── Validate review (optional object) ──
    if 'review' in cfg:
        review = cfg['review']
        if not isinstance(review, dict):
            errors.append(f'"review" must be an object (got {type(review).__name__}).')
        else:
            KNOWN_REVIEW_KEYS = {'mode', 'max_passes', 'reviewer'}
            known_review_list = ', '.join(sorted(KNOWN_REVIEW_KEYS))
            unknown_review = set(review.keys()) - KNOWN_REVIEW_KEYS
            if unknown_review:
                for k in sorted(unknown_review):
                    if k == 'reviewers':
                        errors.append(
                            'Unknown key "review.reviewers". The fan-out reviewer array was removed; '
                            'use a single "review.reviewer" object instead. Run `tusk migrate` to convert '
                            'an existing config.'
                        )
                    else:
                        errors.append(f'Unknown key "review.{k}". Valid review keys: {known_review_list}')

            if 'mode' in review:
                VALID_MODES = {'ai_only', 'disabled'}
                if review['mode'] == 'ai_then_human':
                    errors.append(f'"review.mode" value "ai_then_human" has been removed; use "ai_only" instead.')
                elif review['mode'] not in VALID_MODES:
                    modes_list = ', '.join(sorted(VALID_MODES))
                    errors.append(f'"review.mode" must be one of: {modes_list} (got {review["mode"]!r}).')

            if 'max_passes' in review:
                mp = review['max_passes']
                if not isinstance(mp, int) or isinstance(mp, bool):
                    errors.append(f'"review.max_passes" must be an integer (got {type(mp).__name__}: {mp!r}).')
                elif mp < 1:
                    errors.append(f'"review.max_passes" must be at least 1 (got {mp}).')

            if 'reviewer' in review:
                rv = review['reviewer']
                if rv is None:
                    pass
                elif not isinstance(rv, dict):
                    errors.append(f'"review.reviewer" must be an object with name and description fields (got {type(rv).__name__}: {rv!r}).')
                else:
                    if not isinstance(rv.get('name'), str):
                        errors.append('"review.reviewer.name" must be a string.')
                    if not isinstance(rv.get('description'), str):
                        errors.append('"review.reviewer.description" must be a string.')

    # ── Validate merge (optional object) ──
    if 'merge' in cfg:
        merge = cfg['merge']
        if not isinstance(merge, dict):
            errors.append(f'"merge" must be an object (got {type(merge).__name__}).')
        else:
            KNOWN_MERGE_KEYS = {'mode'}
            known_merge_list = ', '.join(sorted(KNOWN_MERGE_KEYS))
            unknown_merge = set(merge.keys()) - KNOWN_MERGE_KEYS
            if unknown_merge:
                for k in sorted(unknown_merge):
                    errors.append(f'Unknown key "merge.{k}". Valid merge keys: {known_merge_list}')

            if 'mode' in merge:
                VALID_MERGE_MODES = {'local', 'pr'}
                if merge['mode'] not in VALID_MERGE_MODES:
                    modes_list = ', '.join(sorted(VALID_MERGE_MODES))
                    errors.append(f'"merge.mode" must be one of: {modes_list} (got {merge["mode"]!r}).')

    # ── Validate test_command (optional string) ──
    if 'test_command' in cfg:
        tc = cfg['test_command']
        if tc is not None and not isinstance(tc, str):
            errors.append(f'"test_command" must be a string (got {type(tc).__name__}: {tc!r}).')

    # ── Validate test_command_timeout_sec (optional positive integer) ──
    if 'test_command_timeout_sec' in cfg:
        tt = cfg['test_command_timeout_sec']
        if not isinstance(tt, int) or isinstance(tt, bool) or tt <= 0:
            errors.append(
                f'"test_command_timeout_sec" must be a positive integer '
                f'(got {type(tt).__name__}: {tt!r}).'
            )

    # ── Validate baseline_min_sample_size (optional positive integer) ──
    if 'baseline_min_sample_size' in cfg:
        bms = cfg['baseline_min_sample_size']
        if not isinstance(bms, int) or isinstance(bms, bool) or bms <= 0:
            errors.append(
                f'"baseline_min_sample_size" must be a positive integer '
                f'(got {type(bms).__name__}: {bms!r}).'
            )

    # ── Validate path_test_commands (optional object of glob→command strings) ──
    # Insertion order is preserved and matters: tusk-commit.py picks the first
    # pattern where every staged file matches, so users order patterns
    # most-specific-first with an optional "*" catch-all at the end.
    if 'path_test_commands' in cfg:
        ptc = cfg['path_test_commands']
        if not isinstance(ptc, dict):
            errors.append(f'"path_test_commands" must be an object (got {type(ptc).__name__}).')
        else:
            for k, v in ptc.items():
                if not isinstance(k, str) or not k:
                    errors.append(f'"path_test_commands" keys must be non-empty strings (got {type(k).__name__}: {k!r}).')
                if not isinstance(v, str):
                    errors.append(f'"path_test_commands.{k}" value must be a string (got {type(v).__name__}: {v!r}).')

    # ── Report ──
    if errors:
        print(f'Config validation failed ({config_path}):', file=sys.stderr)
        for e in errors:
            print(f'  - {e}', file=sys.stderr)
        return 1

    return 0


# Status transition constraint (separate from value validation).
# Allowed: To Do->In Progress, To Do->Done, In Progress->Done; same-status
# no-ops always allowed. Stored as a constant so both gen-triggers and the
# drift detector use the exact same source text.
_STATUS_TRANSITION_SQL = """CREATE TRIGGER validate_status_transition
BEFORE UPDATE OF status ON tasks
FOR EACH ROW
WHEN NOT (
  OLD.status = NEW.status
  OR (OLD.status = 'To Do' AND NEW.status IN ('In Progress', 'Done'))
  OR (OLD.status = 'In Progress' AND NEW.status = 'Done')
)
BEGIN
  SELECT RAISE(ABORT, 'Invalid status transition. Done is terminal. Allowed: To Do->In Progress, To Do->Done, In Progress->Done. Use ''tusk task-reopen <id> --force'' to reset Done -> To Do, or ''tusk task-unstart <id> --force'' to reverse a cleanly-orphaned In Progress -> To Do.');
END"""


def _value_triggers(column, values, table='tasks'):
    """Build the (insert, update) validation trigger pair for a column.

    Returns a list of (trigger_name, create_trigger_sql) tuples. Each SQL
    string omits the leading newline and trailing semicolon so it matches
    the canonical form sqlite_master.sql stores after CREATE TRIGGER.
    """
    if not values:
        return []
    quoted = ', '.join(f"'{v}'" for v in values)
    label = ', '.join(values)
    prefix = f'{table}_{column}' if table != 'tasks' else column
    insert_name = f'validate_{prefix}_insert'
    update_name = f'validate_{prefix}_update'
    insert_sql = (
        f'CREATE TRIGGER {insert_name}\n'
        f'BEFORE INSERT ON {table} FOR EACH ROW\n'
        f'WHEN NEW.{column} IS NOT NULL AND NEW.{column} NOT IN ({quoted})\n'
        f"BEGIN SELECT RAISE(ABORT, 'Invalid {column}. Must be one of: {label}'); END"
    )
    update_sql = (
        f'CREATE TRIGGER {update_name}\n'
        f'BEFORE UPDATE OF {column} ON {table} FOR EACH ROW\n'
        f'WHEN NEW.{column} IS NOT NULL AND NEW.{column} NOT IN ({quoted})\n'
        f"BEGIN SELECT RAISE(ABORT, 'Invalid {column}. Must be one of: {label}'); END"
    )
    return [(insert_name, insert_sql), (update_name, update_sql)]


def compute_expected_triggers(cfg):
    """Compute the full set of validation triggers the live DB *should* have.

    Returns an ordered list of (trigger_name, create_trigger_sql) tuples.
    Driven entirely by config so the same config edits that change
    cmd_gen_triggers also change what cmd_validate_triggers expects.
    """
    triggers = []
    triggers.extend(_value_triggers(
        'status', cfg.get('statuses', ['To Do', 'In Progress', 'Done'])))
    triggers.extend(_value_triggers(
        'priority', cfg.get('priorities', ['Highest', 'High', 'Medium', 'Low', 'Lowest'])))
    triggers.extend(_value_triggers(
        'closed_reason', cfg.get('closed_reasons', ['completed', 'expired', 'wont_do', 'duplicate'])))

    if cfg.get('domains'):
        triggers.extend(_value_triggers('domain', cfg['domains']))
    if cfg.get('task_types'):
        triggers.extend(_value_triggers('task_type', cfg['task_types']))
    if cfg.get('complexity'):
        triggers.extend(_value_triggers('complexity', cfg['complexity']))
    if cfg.get('blocker_types'):
        triggers.extend(_value_triggers('blocker_type', cfg['blocker_types'], 'external_blockers'))
    if cfg.get('workflows'):
        triggers.extend(_value_triggers('workflow', cfg['workflows']))
    if cfg.get('criterion_types'):
        triggers.extend(_value_triggers('criterion_type', cfg['criterion_types'], 'acceptance_criteria'))
    if cfg.get('review_categories'):
        triggers.extend(_value_triggers('category', cfg['review_categories'], 'review_comments'))
    if cfg.get('review_severities'):
        triggers.extend(_value_triggers('severity', cfg['review_severities'], 'review_comments'))

    triggers.append(('validate_status_transition', _STATUS_TRANSITION_SQL))
    return triggers


def _normalize_sql(sql):
    """Collapse whitespace and strip trailing semicolons for comparison.

    SQLite stores CREATE TRIGGER text in sqlite_master.sql verbatim, but
    minor whitespace differences (e.g. tabs vs spaces, blank lines) would
    otherwise produce false drift positives. Comparing whitespace-collapsed
    forms keeps the check resilient to harmless formatting changes.
    """
    if not sql:
        return ''
    return ' '.join(sql.split()).rstrip(';').strip()


def cmd_gen_triggers(config_path: str) -> int:
    with open(config_path) as f:
        cfg = json.load(f)
    for _name, sql in compute_expected_triggers(cfg):
        # Add the trailing semicolon for sqlite3 to parse the statement
        # boundary; sqlite_master.sql then stores the SQL up to (but not
        # including) the semicolon — matching _normalize_sql's rstrip.
        print(sql + ';')
        print()
    return 0


def cmd_validate_triggers(config_path: str, db_path: str) -> int:
    """Detect drift between the validation triggers config says should
    exist and the triggers actually present in the live SQLite DB.

    Exit codes:
        0 — no drift, or DB does not exist (nothing to compare against)
        1 — at least one missing, stale, or unexpected validate_* trigger
    """
    if not os.path.exists(db_path):
        return 0

    with open(config_path) as f:
        cfg = json.load(f)
    expected = {name: _normalize_sql(sql) for name, sql in compute_expected_triggers(cfg)}

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name LIKE 'validate_%'"
        ).fetchall()
    finally:
        conn.close()
    actual = {name: _normalize_sql(sql) for name, sql in rows}

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    stale = sorted(
        name for name in (set(expected) & set(actual))
        if expected[name] != actual[name]
    )

    if not (missing or extra or stale):
        return 0

    print(f'Trigger drift detected ({db_path}):', file=sys.stderr)
    for name in missing:
        print(f'  - missing trigger: {name}', file=sys.stderr)
    for name in stale:
        print(f'  - stale trigger: {name} (live SQL differs from config)', file=sys.stderr)
    for name in extra:
        print(f'  - unexpected trigger: {name} (not produced by current config)', file=sys.stderr)
    print(
        "Run 'tusk regen-triggers' to rebuild validation triggers from config.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    if len(sys.argv) < 3:
        print(
            f'Usage: {sys.argv[0]} <validate|gen-triggers|validate-triggers> <config_path> [db_path]',
            file=sys.stderr,
        )
        return 1

    subcmd = sys.argv[1]
    config_path = sys.argv[2]

    if subcmd == 'validate':
        return cmd_validate(config_path)
    elif subcmd == 'gen-triggers':
        return cmd_gen_triggers(config_path)
    elif subcmd == 'validate-triggers':
        if len(sys.argv) < 4:
            print(
                f'Usage: {sys.argv[0]} validate-triggers <config_path> <db_path>',
                file=sys.stderr,
            )
            return 1
        return cmd_validate_triggers(config_path, sys.argv[3])
    else:
        print(
            f'Unknown subcommand: {subcmd!r}. Expected validate, gen-triggers, or validate-triggers.',
            file=sys.stderr,
        )
        return 1


if __name__ == '__main__':
    sys.exit(main())
