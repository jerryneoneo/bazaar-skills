#!/usr/bin/env python3
import json, glob, os, sys

results = []
for f in sorted(glob.glob('data/threads/fb:*.json')):
    if '.lock' in f:
        continue
    try:
        d = json.load(open(f))
        status = d.get('status', 'active')
        if status in ('lost', 'handover'):
            continue
        cursor = d.get('cursor', {})
        last_ts = cursor.get('last_handled_ts', '')
        transcript = d.get('transcript', [])
        new_msgs = [m for m in transcript if m.get('dir') == 'in' and m.get('ts', '') > last_ts]
        if not last_ts:
            new_msgs = [m for m in transcript if m.get('dir') == 'in']
        if new_msgs:
            results.append({
                'file': f,
                'thread_id': d.get('thread_id', os.path.basename(f).replace('.json', '')),
                'status': status,
                'item_id': d.get('item_id', '?'),
                'buyer': d.get('buyer_handle', '?'),
                'new_count': len(new_msgs),
                'last_in_ts': new_msgs[-1].get('ts', ''),
                'last_msg': new_msgs[-1].get('text', '')[:80],
            })
    except Exception as e:
        print(f'ERR {f}: {e}', file=sys.stderr)

results.sort(key=lambda x: x['last_in_ts'], reverse=True)
for r in results:
    print(json.dumps(r))
