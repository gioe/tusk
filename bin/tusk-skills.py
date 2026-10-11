#!/usr/bin/env python3
"""Install/check native Codex skills from the release bundled with this CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile

STATE = 'tusk/native-skills.json'


def bundle_path():
    here = Path(__file__).resolve().parent
    source = here.parent / 'codex-skills'
    return source if (source / 'catalog.json').is_file() else here / 'codex-skills'


def safe(root, path):
    return path.is_relative_to(root) and not any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tusk-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def state(root):
    path = root / STATE
    if not safe(root, path):
        raise ValueError(f'unsafe skill ownership state: {path}')
    if not path.exists():
        return {'version': 1, 'groups': ['core'], 'files': {}}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('files'), dict):
        raise ValueError(f'unrecognized skill ownership state: {path}')
    for rel, sha in value['files'].items():
        parts = Path(rel).parts
        if (len(parts) < 4 or parts[:2] != ('.agents', 'skills') or '..' in parts
                or not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)):
            raise ValueError(f'invalid skill ownership record: {rel}')
    return value


def expected(bundle, groups):
    catalog = json.loads((bundle / 'catalog.json').read_text())
    choices = catalog['groups']
    if not isinstance(groups, list) or not all(isinstance(g, str) and g in choices for g in groups):
        raise ValueError('skill groups must be selected from: ' + ', '.join(choices))
    files = {}
    for group in groups:
        for name in choices[group]:
            if not re.fullmatch('[a-z0-9-]+', name):
                raise ValueError('invalid bundled skill name')
            folder = bundle / name
            if not (folder / 'SKILL.md').is_file():
                raise ValueError(f'bundle is missing {name}/SKILL.md')
            for path in sorted(folder.rglob('*')):
                if path.is_file():
                    if not safe(bundle, path):
                        raise ValueError(f'unsafe bundled asset: {path}')
                    files[f'.agents/skills/{name}/{path.relative_to(folder)}'] = path.read_bytes()
            files[f'.agents/skills/{name}/references/runtime.md'] = (bundle / '_shared/runtime.md').read_bytes()
    return files


def ignore_generated(root, files):
    # Exact local runtime files only; tracked files remain tracked, and unrelated
    # skills/custom additions are never hidden by a directory-wide ignore.
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--git-path', 'info/exclude'],
                            capture_output=True, text=True, encoding='utf-8', timeout=5)
    if result.returncode:
        return
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = root / path
    if path.is_symlink():
        return
    text = path.read_text() if path.exists() else ''
    existing = set(text.splitlines())
    additions = ['/' + rel for rel in sorted([STATE, *files]) if '/' + rel not in existing]
    if additions:
        write_atomic(path, (text.rstrip('\n') + '\n' + '\n'.join(additions) + '\n').encode())


def reconcile(root, bundle=None, groups=None, check=False, selection_root=None):
    root = Path(root).resolve()
    bundle = Path(bundle or bundle_path()).resolve()
    saved = state(root)
    selection = state(Path(selection_root).resolve()) if selection_root else saved
    groups = groups if groups is not None else selection.get('groups', ['core'])
    desired = expected(bundle, groups)
    owned = dict(saved['files'])
    legacy_path = root / 'tusk/codex-skill-mirrors.json'
    if safe(root, legacy_path) and legacy_path.is_file():
        try:
            legacy = json.loads(legacy_path.read_text())
            if legacy.get('version') == 1 and isinstance(legacy.get('files'), dict):
                for rel, sha in legacy['files'].items():
                    target = root / rel
                    if (rel in desired and rel not in owned and safe(root, target)
                            and target.is_file() and digest(target.read_bytes()) == sha):
                        owned[rel] = sha
        except (OSError, ValueError, AttributeError):
            pass
    issues, changed = [], []
    # An existing unowned skill is a complete customization boundary. Do not add
    # our references to someone else's SKILL.md merely because its name matches.
    blocked = set()
    for rel in desired:
        skill = Path(*Path(rel).parts[:3])
        entry = root / skill / 'SKILL.md'
        entry_rel = str(skill / 'SKILL.md')
        if entry.exists() and entry_rel not in owned and entry.read_bytes() != desired[entry_rel]:
            blocked.add(str(skill))
    for rel, data in desired.items():
        target = root / rel
        if not safe(root, target) or str(Path(*Path(rel).parts[:3])) in blocked:
            issues.append(f'custom or symlinked skill preserved: {rel}')
            continue
        current = target.read_bytes() if target.is_file() else None
        if target.exists() and current is None:
            issues.append(f'non-file preserved: {rel}')
            continue
        if current != data:
            if current is not None and digest(current) != owned.get(rel):
                issues.append(f'local modification preserved: {rel}')
                continue
            if check:
                issues.append(f'missing or outdated: {rel}; run tusk skills install')
                continue
            write_atomic(target, data)
            changed.append(rel)
        if not check:
            owned[rel] = digest(data)
    for rel in set(owned) - set(desired):
        target = root / rel
        if not safe(root, target):
            issues.append(f'symlinked retired skill file preserved: {rel}')
        elif target.is_file() and digest(target.read_bytes()) != owned[rel]:
            issues.append(f'custom retired skill file preserved: {rel}')
        elif check:
            issues.append(f'retired managed file: {rel}; run tusk skills install')
        else:
            if target.is_file():
                target.unlink()
                changed.append(rel)
            owned.pop(rel)
    if not check:
        ignore_generated(root, owned)
        write_atomic(root / STATE, (json.dumps({'version': 1, 'groups': groups, 'files': owned}, indent=2, sort_keys=True) + '\n').encode())
    return {'groups': groups, 'changed': changed, 'issues': sorted(set(issues))}


def discovery_issues(root, include_user=True):
    root = Path(root).resolve()
    locations = [root / '.agents/skills']
    if include_user:
        locations.extend([Path.home() / '.agents/skills', Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills'])
    names, issues, seen = {}, [], set()
    for base in locations:
        for entry in sorted(base.glob('*/SKILL.md')):
            if entry.resolve() in seen:
                continue
            seen.add(entry.resolve())
            try:
                body = entry.read_text()
            except OSError:
                continue
            match = re.search(r'^name:\s*[\'\"]?([a-z0-9-]+)', body, re.M)
            if match:
                names.setdefault(match[1], []).append(str(entry))
            if base == locations[0]:
                for link in re.findall(r'\]\(([^)]+)\)', body):
                    link = link.split('#', 1)[0]
                    if link and not re.match(r'[a-z]+:', link) and not (entry.parent / link).exists():
                        issues.append(f'broken reference in {entry}: {link}')
    for name, paths in names.items():
        if len(paths) > 1:
            issues.append(f'duplicate skill name {name}: ' + ', '.join(paths))
    return issues


def seed_worktree(primary, workspace, bundle=None):
    """Seed only installations that opted into native skills; preserve tracked customizations."""
    primary = Path(primary)
    if not (primary / STATE).is_file():
        return
    workspace = Path(workspace).resolve()
    # Carry the actual primary copies, including local edits to managed skills.
    # Existing tracked/custom workspace files remain authoritative.
    for rel in state(primary)['files']:
        source, target = primary / rel, workspace / rel
        if safe(primary, source) and safe(workspace, target) and source.is_file() and not target.exists():
            write_atomic(target, source.read_bytes())
    result = reconcile(workspace, bundle=bundle, selection_root=primary)
    if result['issues']:
        print('Native skills: ' + '; '.join(result['issues']), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('root')
    parser.add_argument('command', choices=['install', 'check'])
    parser.add_argument('--groups', help='Replacement group selection, e.g. core,reporting; persisted on install')
    parser.add_argument('--source', type=Path, help='Release bundle directory (installer use)')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    args = parser.parse_args()
    try:
        if args.command == 'check' and args.groups is not None:
            raise ValueError('--groups is only supported with install')
        groups = [g for g in args.groups.split(',') if g] if args.groups is not None else None
        result = reconcile(args.root, args.source, groups, check=args.command == 'check')
        result['issues'].extend(discovery_issues(args.root))
        if args.format == 'json':
            print(json.dumps(result))
        else:
            print(f"Native skills: {len(result['changed'])} files updated; groups: {', '.join(result['groups']) or '(none)'}")
            for issue in result['issues']:
                print('  ' + issue)
        return int(bool(result['issues']) and args.command == 'check')
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'tusk skills: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
