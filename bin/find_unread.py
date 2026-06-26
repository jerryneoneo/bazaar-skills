import json, os, glob

threads_dir = "data/threads"
unread = []
for fpath in glob.glob(os.path.join(threads_dir, "*.json")):
    try:
        d = json.load(open(fpath))
        status = d.get("status", "active")
        if status in ("lost", "handover"):
            continue
        transcript = d.get("transcript", [])
        cursor = d.get("cursor", {}).get("last_handled_msg_id", "")
        inbound = [m for m in transcript if m.get("dir") == "in"]
        if inbound:
            last_in = inbound[-1]
            if last_in.get("msg_id") != cursor:
                unread.append({
                    "file": fpath,
                    "last_in_id": last_in.get("msg_id"),
                    "last_in_ts": last_in.get("ts", 0),
                    "last_in_text": last_in.get("text", ""),
                    "cursor": cursor,
                    "status": status
                })
    except Exception as e:
        pass

unread.sort(key=lambda x: x["last_in_ts"], reverse=True)
for u in unread:
    print(u)
