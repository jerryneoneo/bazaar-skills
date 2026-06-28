import json, os, glob, sys

market = sys.argv[1] if len(sys.argv) > 1 else "fb"
threads_dir = "data/threads"
pending = []

for f in sorted(glob.glob(f"{threads_dir}/{market}:*.json")):
    try:
        with open(f) as fh:
            t = json.load(fh)
        status = t.get("status", "active")
        if status in ("lost", "handover"):
            continue
        transcript = t.get("transcript", [])
        cursor_id = t.get("cursor", {}).get("last_handled_msg_id")

        found_cursor = cursor_id is None
        new_msgs = []
        for msg in transcript:
            if not found_cursor:
                if msg.get("msg_id") == cursor_id:
                    found_cursor = True
                continue
            if msg.get("dir") == "in":
                new_msgs.append(msg)

        if new_msgs:
            pending.append({
                "thread_id": t.get("thread_id"),
                "item_id": t.get("item_id"),
                "buyer_handle": t.get("buyer_handle"),
                "status": status,
                "new_msgs": new_msgs,
                "file": f
            })
    except Exception as e:
        print(f"ERR {f}: {e}", file=sys.stderr)

print(json.dumps(pending, indent=2))
