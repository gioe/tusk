#!/usr/bin/env python3
"""Bounded, read-only traversal of recorded provenance relationships."""
import argparse
from collections import deque
import json
import sqlite3
import sys

import tusk_loader

p = tusk_loader.load('tusk-provenance')
db = tusk_loader.load('tusk-db-lib')
json_lib = tusk_loader.load('tusk-json-lib')


def bounded(value, clipped):
    if isinstance(value, str) and len(value) > 400:
        clipped.append(True)
        return value[:400] + '…'
    if isinstance(value, dict):
        return {k: bounded(v, clipped) for k, v in value.items()}
    return value


def node(conn, row):
    result = dict(row, ref=p.reference(p.project_id(conn), row['id']))
    details = {}
    if row['kind'] in p.NATIVE_V89 and row['availability'] == 'available':
        native = conn.execute(f"SELECT * FROM {p.NATIVE_V89[row['kind']]} WHERE id=?", (row['native_id'],)).fetchone()
        if native is None:
            result['availability'] = 'missing'
        else:
            fields = ('summary','status','criterion','is_completed','item_type','content',
                      'comment','resolution','diff_range','note','next_steps')
            details = {k: native[k] for k in fields if k in native.keys()}
    elif row['kind'] == 'prompt':
        details = p.prompt_snapshot(conn, row['id']) or {}
    elif row['kind'] == 'artifact':
        details = tusk_loader.load('tusk-evidence-lib').output(conn, row) or {}
    elif row['kind'] == 'evidence':
        proof = tusk_loader.load('tusk-evidence-lib').output(conn, row)
        if proof:
            details = {k: v for k, v in proof.items() if k not in ('details','result')}
            details['target'] = proof['details'].get('target')
            final = proof['result']
            details['result'] = {k: v for k, v in final.items() if k != 'payload'} if final else None
    elif row['kind'] == 'action':
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='provenance_actions'").fetchone():
            receipt = conn.execute('SELECT command,outcome,context_json FROM provenance_actions WHERE record_id=?', (row['id'],)).fetchone()
            if receipt:
                details = dict(receipt)
                details['context'] = json.loads(details.pop('context_json'))
                details['is_verification'] = False
    clipped = []
    result['details'] = bounded(details, clipped)
    for field in ('external_key','locator'):
        result[field] = bounded(result[field], clipped)
    result['content_truncated'] = bool(clipped)
    return result


def trace(conn, reference, direction, depth, limit):
    root = p.fetch_record(conn, reference)
    nodes = {root['id']: node(conn, root)}
    edges = {}
    queue = deque([(root['id'], 0)])
    reasons = set()
    while queue:
        rid, level = queue.popleft()
        # Most links read dependent -> basis. Supports reads basis -> dependent.
        if direction == 'both':
            where = '(source_id=? OR target_id=?)'
        else:
            forward, reverse = ('source_id', 'target_id') if direction == 'ancestors' else ('target_id', 'source_id')
            where = f"(({forward}=? AND relationship <> 'supports') OR ({reverse}=? AND relationship='supports'))"
        params = [rid, rid]
        if edges:
            # IDs come only from SQLite rows; avoid exceeding older SQLite's
            # 999 bound-parameter ceiling at the documented 1000-edge limit.
            where += ' AND id NOT IN (' + ','.join(str(int(i)) for i in edges) + ')'
        room = limit - len(edges)
        rows = conn.execute(f'SELECT * FROM provenance_links WHERE {where} ORDER BY id LIMIT ?',
                            (*params, 1 if level == depth else room + 1)).fetchall()
        if level == depth:
            if rows:
                reasons.add('depth')
            continue
        if len(rows) > room:
            reasons.add('limit')
        for edge in rows[:room]:
            other = edge['target_id'] if edge['source_id'] == rid else edge['source_id']
            if other not in nodes:
                if len(nodes) == limit:
                    reasons.add('limit')
                    continue
                endpoint = conn.execute('SELECT * FROM provenance_records WHERE id=?', (other,)).fetchone()
                if endpoint is None:
                    nodes[other] = {'id': other, 'ref': p.reference(p.project_id(conn), other),
                                    'kind': 'unknown', 'availability': 'missing', 'details': {}, 'content_truncated': False}
                else:
                    nodes[other] = node(conn, endpoint)
                    queue.append((other, level + 1))
            edge_out = p.link_output(conn, edge)
            clipped = []
            edge_out['reason'] = bounded(edge_out['reason'], clipped)
            edge_out['content_truncated'] = bool(clipped)
            edges[edge['id']] = edge_out
    return {'root': reference, 'direction': direction, 'depth': depth, 'limit': limit,
            'nodes': list(nodes.values()), 'links': list(edges.values()),
            'truncated': bool(reasons), 'truncation_reasons': sorted(reasons),
            'scope': 'recorded_links_only'}


def render(data):
    lines = [f"Trace {data['root']} ({data['direction']}; recorded links only)"]
    for n in data['nodes']:
        lines.append(f"{n['ref']} {n['kind']} [{n['availability']}] " + json.dumps(n['details'], ensure_ascii=True))
        if n['content_truncated']:
            lines.append('  Content truncated; use provenance get for full details.')
    for e in data['links']:
        lines.append(f"{e['source_ref']} --{e['relationship']} [{e['attribution']}]--> {e['target_ref']}"
                     + (' reason=' + json.dumps(e['reason'], ensure_ascii=True) if e['reason'] else '')
                     + (' [content truncated]' if e['content_truncated'] else ''))
    lines.append('Truncated: ' + (', '.join(data['truncation_reasons']) if data['truncated'] else 'no'))
    return '\n'.join(lines)


def main(argv):
    parser = argparse.ArgumentParser(prog='tusk trace', allow_abbrev=False, description='Read recorded provenance links, never inferred session/timestamp associations. Ancestors follow links toward their basis; dependents reverse that walk. Supports is basis-to-dependent, so its direction is reversed for ancestors. Original edge labels and orientation are preserved.')
    parser.add_argument('reference', help='Full tusk:<project-id>:<record-id> reference; never registers on read.')
    parser.add_argument('--direction', choices=('ancestors','dependents','both'), default='ancestors')
    parser.add_argument('--depth', type=int, default=3, help='Maximum hops, 0..20 (default 3).')
    parser.add_argument('--limit', type=int, default=50, help='Maximum nodes AND links, 1..1000 (default 50; root counts).')
    parser.add_argument('--format', choices=('json','text'), default='json')
    args = parser.parse_args(argv[1:])
    if not 0 <= args.depth <= 20 or not 1 <= args.limit <= 1000:
        parser.error('depth must be 0..20 and limit must be 1..1000')
    conn = db.get_connection(argv[0])
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        data = trace(conn, args.reference, args.direction, args.depth, args.limit)
        print(json_lib.dumps(data) if args.format == 'json' else render(data))
        return 0
    except (ValueError, sqlite3.Error) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
