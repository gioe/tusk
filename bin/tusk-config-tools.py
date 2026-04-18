#!/usr/bin/env python3
"""Config validation and trigger generation for tusk.

Called by the tusk wrapper:
    tusk validate         → tusk-config-tools.py validate <config_path>
    tusk regen-triggers   → tusk-config-tools.py gen-triggers <config_path>

Arguments:
    sys.argv[1] — subcommand: 'validate' or 'gen-triggers'
    sys.argv[2] — path to the resolved config JSON file
"""

import json
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
    KNOWN_KEYS = {'domains', 'task_types', 'statuses', 'priorities', 'closed_reasons', 'complexity', 'blocker_types', 'criterion_types', 'workflows', 'agents', 'dupes', 'review', 'review_categories', 'review_severities', 'merge', 'test_command', 'test_command_timeout_sec', 'domain_test_commands', 'project_type', 'project_libs', 'issue_scoring'}
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
            KNOWN_DUPE_KEYS = {'strip_prefixes', 'check_threshold', 'similar_threshold'}
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

            for thresh in ('check_threshold', 'similar_threshold'):
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

    # ── Report ──
    if errors:
        print(f'Config validation failed ({config_path}):', file=sys.stderr)
        for e in errors:
            print(f'  - {e}', file=sys.stderr)
        return 1

    return 0


def cmd_gen_triggers(config_path: str) -> int:
    with open(config_path) as f:
        cfg = json.load(f)

    def trigger_sql(column, values, table='tasks'):
        if not values:
            return ''
        quoted = ', '.join(f"'{v}'" for v in values)
        label = ', '.join(values)
        prefix = f'{table}_{column}' if table != 'tasks' else column
        return f'''
CREATE TRIGGER validate_{prefix}_insert
BEFORE INSERT ON {table} FOR EACH ROW
WHEN NEW.{column} IS NOT NULL AND NEW.{column} NOT IN ({quoted})
BEGIN SELECT RAISE(ABORT, 'Invalid {column}. Must be one of: {label}'); END;

CREATE TRIGGER validate_{prefix}_update
BEFORE UPDATE OF {column} ON {table} FOR EACH ROW
WHEN NEW.{column} IS NOT NULL AND NEW.{column} NOT IN ({quoted})
BEGIN SELECT RAISE(ABORT, 'Invalid {column}. Must be one of: {label}'); END;
'''

    # Always enforce these
    print(trigger_sql('status', cfg.get('statuses', ['To Do', 'In Progress', 'Done'])))
    print(trigger_sql('priority', cfg.get('priorities', ['Highest', 'High', 'Medium', 'Low', 'Lowest'])))
    print(trigger_sql('closed_reason', cfg.get('closed_reasons', ['completed', 'expired', 'wont_do', 'duplicate'])))

    # Only enforce if configured
    domains = cfg.get('domains', [])
    if domains:
        print(trigger_sql('domain', domains))

    task_types = cfg.get('task_types', [])
    if task_types:
        print(trigger_sql('task_type', task_types))

    complexity = cfg.get('complexity', [])
    if complexity:
        print(trigger_sql('complexity', complexity))

    blocker_types = cfg.get('blocker_types', [])
    if blocker_types:
        print(trigger_sql('blocker_type', blocker_types, 'external_blockers'))

    workflows = cfg.get('workflows', [])
    if workflows:
        print(trigger_sql('workflow', workflows))

    criterion_types = cfg.get('criterion_types', [])
    if criterion_types:
        print(trigger_sql('criterion_type', criterion_types, 'acceptance_criteria'))

    review_categories = cfg.get('review_categories', [])
    if review_categories:
        print(trigger_sql('category', review_categories, 'review_comments'))

    review_severities = cfg.get('review_severities', [])
    if review_severities:
        print(trigger_sql('severity', review_severities, 'review_comments'))

    # Status transition constraint (separate from value validation)
    # Allowed: To Do->In Progress, To Do->Done, In Progress->Done; same-status no-ops always allowed
    print('''
CREATE TRIGGER validate_status_transition
BEFORE UPDATE OF status ON tasks
FOR EACH ROW
WHEN NOT (
  OLD.status = NEW.status
  OR (OLD.status = 'To Do' AND NEW.status IN ('In Progress', 'Done'))
  OR (OLD.status = 'In Progress' AND NEW.status = 'Done')
)
BEGIN
  SELECT RAISE(ABORT, 'Invalid status transition. Done is terminal. Allowed: To Do->In Progress, To Do->Done, In Progress->Done');
END;
''')

    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(f'Usage: {sys.argv[0]} <validate|gen-triggers> <config_path>', file=sys.stderr)
        return 1

    subcmd = sys.argv[1]
    config_path = sys.argv[2]

    if subcmd == 'validate':
        return cmd_validate(config_path)
    elif subcmd == 'gen-triggers':
        return cmd_gen_triggers(config_path)
    else:
        print(f'Unknown subcommand: {subcmd!r}. Expected validate or gen-triggers.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
