import json

with open('data/escalations.jsonl') as f:
    lines = f.readlines()

print("=== Venus escalations ===")
for line in lines:
    try:
        e = json.loads(line)
        if 'venus' in e.get('thread_id','').lower() or 'venus' in e.get('buyer_handle','').lower():
            print(json.dumps(e, indent=2))
    except Exception as ex:
        pass

print("\n=== Open escalations ===")
for line in lines:
    try:
        e = json.loads(line)
        if e.get('status') == 'open':
            print(json.dumps(e, indent=2))
    except Exception as ex:
        pass

print("\n=== Channel state pending ===")
try:
    with open('data/channel_state.json') as f:
        cs = json.load(f)
    pending = cs.get('pending', [])
    print(f'Pending items: {len(pending)}')
    for p in pending:
        print(json.dumps(p, indent=2))
except Exception as ex:
    print(f'Error: {ex}')
