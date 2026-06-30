import json, glob, sys

threads_dir = "data/threads"
fb_threads = []

for f in sorted(glob.glob(f"{threads_dir}/fb:*.json")):
    if f.endswith(".lock"):
        continue
    try:
        with open(f) as fh:
            t = json.load(fh)
    except Exception as e:
        print(f"SKIP {f}: {e}", file=sys.stderr)
        continue
    status = t.get("status", "active")
    if status in ("lost", "handover"):
        continue
    cursor = t.get("cursor", {})
    last_handled = cursor.get("last_handled_msg_id")
    transcript = t.get("transcript", [])
    past_cursor = False
    if not transcript:
        continue
    if last_handled is None:
        if any(m.get("dir") == "in" for m in transcript):
            past_cursor = True
    else:
        found = False
        for i, msg in enumerate(transcript):
            if msg.get("msg_id") == last_handled:
                found = True
                remaining = transcript[i+1:]
                if any(m.get("dir") == "in" for m in remaining):
                    past_cursor = True
                break
        if not found:
            # cursor ID not in transcript - treat as unread
            if any(m.get("dir") == "in" for m in transcript):
                past_cursor = True
    if past_cursor:
        last_in = next((m for m in reversed(transcript) if m.get("dir") == "in"), None)
        fb_threads.append({
            "thread_id": t.get("thread_id"),
            "buyer": t.get("buyer_handle"),
            "item_id": t.get("item_id"),
            "status": status,
            "last_handled": last_handled,
            "last_inbound": last_in.get("text", "")[:100] if last_in else None,
        })

print(json.dumps(fb_threads, indent=2))
