# DELIST — seller-initiated take-down of a live listing

Handles a direct seller request to remove a listing that is **already live** ("delete the
mouse everywhere", "take down the Sony", "/delist"). This is **not** a sale completion — for
"a buyer paid, close it out" use `notifications.md` (sale → done), which runs the same
per-platform take-down recipe but is triggered by `bin/negotiate.py confirm-sold`.

> **Why this file exists (the bug it fixes):** there used to be no spec for a seller-initiated
> delete. The model improvised, and with no active listing session it wrote the deletion marker
> into the transient single-slot `data/listing_session.json` instead of the durable item record —
> so `data/items/<item_id>.json` stayed `status:"live"` and the agent kept reporting a dead
> listing as live. The durable item record is the ONLY source of truth for "is it listed". This
> flow always writes there, via `bin/delist_item.py`, never to a session file.

## Resolve the item (never trust the session slot)
1. Identify the item from the seller's words. Match against `data/items/*.json` by title /
   `item_id` (e.g. "the mouse" → `logitech-mx-master-3`). If it's ambiguous, `ask()` ONE
   question listing the live candidates; if nothing matches a managed item, `say()` so and stop.
2. Load that item's `data/items/<item_id>.json`. The `listing_urls` map tells you which
   platforms to take down. **Resolve by item_id — do NOT read `listing_session.json` to decide
   what is listed.** A delist can arrive with no active session, mid an unrelated session, or
   while a distribution pass runs; none of that changes which item the seller named.
3. If the item is already `removed_by_seller` / `sold`, `say()` it's already down and stop
   (idempotent — don't re-run browser take-downs on a gone listing).

## Take down each platform (browser), then verify
For each `market` in `items.listing_urls`:
- Follow `skills/listing-flows/<market>.md` **Take-down recipe** (e.g. Carousell: open the manage
  menu → "Delete"/"Mark as sold" → confirm; FB: ••• → "Delete listing" → confirm).
- **Verify** the listing is actually gone (recipe confirms 0 matches / 404 / redirect to profile).
  Logged-out / checkpoint / captcha → **stop and escalate** re-auth (`notifications.md`); do not
  tight-loop retry. If one platform fails, take down the others and report a partial result —
  do NOT mark the durable record removed until every listed platform is confirmed gone (see below).

## Write the durable record (the canonical step)
Once **all** of `items.listing_urls` are confirmed down:
```
python3 bin/delist_item.py <item_id> [--reason "seller request"]
```
This transitions `data/items/<item_id>.json`: archives `listing_urls` → `removed_urls`, clears
`listing_urls` to `{}`, sets `status:"removed_by_seller"` and `removed_at`. One canonical status
string, written to the durable file. It is idempotent and never loses the archived URLs (so a
later relist can reference what was there).

- **Partial take-down** (one platform stuck on re-auth): do NOT run `delist_item.py` yet — the
  item is still live somewhere. Take down what you can, escalate the stuck platform, and leave the
  record `live`. Re-run this flow after re-auth. (A future enhancement may track per-platform
  removal; today the durable flip means "gone everywhere".)

## Confirm to the seller
`say()` what came down, per platform, and that it's no longer tracked as listed. If the seller is
delisting in order to **re-list** (a fresh listing), they can now `/list` it cleanly — the durable
record is `removed_by_seller`, so listing.md starts a new draft instead of colliding with a stale
`live` record.

## Guardrails
- Source of truth for "is it listed" is `data/items/<item_id>.json`, never a session file.
- Never compose or guess a listing URL; take-down navigates the URL already recorded.
- `--dry-run` on the platform recipe → log the verbs instead of executing (rehearse safely).
