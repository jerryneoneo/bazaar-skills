import json, glob, os, sys

market = sys.argv[1] if len(sys.argv) > 1 else "fb"
for f in sorted(glob.glob(f"data/threads/{market}:*.json")):
    t = json.load(open(f))
    tr = t.get("transcript", [])
    last = tr[-1] if tr else None
    cursor = t.get("cursor", {}).get("last_handled_msg_id")
    status = t.get("status", "active")
    last_id = last.get("msg_id") if last else None
    last_dir = last.get("dir") if last else None
    tid = t.get("thread_id", "?")[:35]
    print(f"{tid:35s} status={status:10s} cursor={str(cursor)[:20]:20s} last={str(last_id)[:20]:20s} dir={last_dir}")
