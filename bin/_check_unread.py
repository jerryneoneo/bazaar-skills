import json, os, glob, sys

market = sys.argv[1] if len(sys.argv) > 1 else 'fb'
threads_dir = 'data/threads'
unread = []

for f in sorted(glob.glob(f'{threads_dir}/{market}:*.json')):
    if f.endswith('.lock'):
        continue
    try:
        with open(f) as fh:
            t = json.load(fh)
        status = t.get('status', 'active')
        if status in ('lost', 'handover'):
            continue
        transcript = t.get('transcript', [])
        cursor_id = t.get('cursor', {}).get('last_handled_msg_id')
        past_cursor = (cursor_id is None)
        new_inbound = []
        for msg in transcript:
            if not past_cursor:
                if msg.get('msg_id') == cursor_id:
                    past_cursor = True
                continue
            if msg.get('dir') == 'in':
                new_inbound.append(msg)
        if new_inbound:
            buyer = t.get('buyer_handle', os.path.basename(f))
            tid = t.get('thread_id', os.path.basename(f).replace('.json', ''))
            unread.append({
                'thread_id': tid,
                'buyer': buyer,
                'status': status,
                'new_msgs': len(new_inbound),
                'last': new_inbound[-1].get('text', '')[:100]
            })
    except Exception as e:
        print(f'ERR {f}: {e}', file=sys.stderr)

print(json.dumps(unread, indent=2))
