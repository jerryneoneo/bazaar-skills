import json, os, glob, sys

threads_dir = "data/threads"
pending = []

for fname in sorted(glob.glob(f"{threads_dir}/fb:*.json")):
    try:
        with open(fname) as f:
            t = json.load(f)

        status = t.get("status", "active")
        if status in ("lost", "handover"):
            continue

        transcript = t.get("transcript", [])
        cursor_id = t.get("cursor", {}).get("last_handled_msg_id")

        if cursor_id is None:
            unhandled = [m for m in transcript if m.get("dir") == "in"]
        else:
            past_cursor = False
            unhandled = []
            for m in transcript:
                if m.get("msg_id") == cursor_id:
                    past_cursor = True
                    continue
                if past_cursor and m.get("dir") == "in":
                    unhandled.append(m)

        if unhandled:
            pending.append({
                "file": fname,
                "thread_id": t.get("thread_id"),
                "item_id": t.get("item_id"),
                "buyer": t.get("buyer_handle", ""),
                "status": status,
                "unhandled": unhandled,
                "cursor_id": cursor_id
            })
    except Exception as e:
        print(f"ERROR {fname}: {e}")

print(f"Found {len(pending)} thread(s) with unhandled messages:")
for p in pending:
    print(f"  {p['thread_id']} | item:{p['item_id']} | buyer:{p['buyer']} | {len(p['unhandled'])} new msg(s)")
    for m in p['unhandled']:
        print(f"    [{m.get('msg_id')}] {m.get('text','')[:120]}")
