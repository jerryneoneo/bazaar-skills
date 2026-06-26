# Browser actions — the bridge vocabulary

Every command in this seller-agent drives **a marketplace** (any of the seller's enabled platforms —
FB Marketplace, Carousell, eBay, …) through **your real, logged-in Chrome session** (Claude in
Chrome). The commands never name a specific automation library *or a specific site* — they reference
only the small vocabulary below. Whatever bridge is wired in just has to satisfy these actions. Today
that bridge is **Claude in Chrome**; every marketplace satisfies the same verbs (only the field names
on the page differ — those live in `skills/listing-flows/<market>.md`).

> **Why real-session only.** Acting as you, in your own Chrome with your own logged-in
> cookies, at human pacing, is the account-safety thesis. Never run these against a
> fresh/headless browser or a server — that gets fingerprinted and banned.

## The actions

| Action | Meaning | Returns |
|---|---|---|
| `navigate(target)` | Go to a place: a marketplace's create/sell flow, its inbox, a thread, a search, a listing URL. | page is loaded |
| `read_inbox()` | Read the marketplace's inbox/chat list. | list of `{thread_id, buyer_handle, item_hint, unread, last_snippet}` |
| `read_thread(thread_id)` | Open one conversation and read its messages. | ordered list of `{msg_id, dir:"in"\|"out", text, ts}` |
| `read_listing()` | Read listing rows on a page: a search-results page (for live price comps, or the buyer-side `skills/buying/search.md`) or the seller's own "your listings / selling" page (for `distribution.md` SCAN). Returns each row's title, price, its permalink `url` (the dedupe key), and `sold?`. On a **buyer search page** also return any of `{thumbnail, location, distance_km, seller_handle, condition, posted_time}` the row exposes (omit/`null` when absent). | list of `{title, price, url, sold?, thumbnail?, location?, distance_km?, seller_handle?, condition?, posted_time?}` |
| `type(field, text)` | Type into a named field/box (title, price, description, message box). | text entered |
| `click(target)` | Click a named control (next, publish/list, category option, send-attachment). | control activated |
| `send()` | Send the currently-typed message in an open thread. | message sent |

Photo upload during listing is a `click("add-photos")` followed by selecting the
local file paths from the item record — treated as a `click`/OS-picker step.

## How Claude satisfies these (goal-style, not scripted)

You are the adaptation layer. Don't hard-code selectors. For each action, **look at
the page and find the control the way a human would** (the create-listing "Next"
button, the message text box, the unread dot). If the site moved a button, re-find it —
a moved button is re-found, not a break. This absorbs marketplace DOM volatility
(feasibility §2.3). Reliability caveat: actions that publish or send go through the
pacing/confirmation rules in `reply-pipeline.md` — never improvise a send.

## Page memory (a hint, never a crutch)

A per-market selector cache (`data/ui_cache/<market>/<flow>.json`, via `bin/ui_cache.py`) remembers
where each listing control was last found, so a routine listing can skip the `browser_snapshot` +
vision round-trip per field — snapshots are the single biggest token cost per pass. **The cache only
ever makes finding a control faster. It never decides whether to act, never decides what to type, and
is never trusted blindly.** It changes only *where* a control is.

For each `type`/`click` control in a listing recipe:

1. `python3 bin/ui_cache.py get --market <m> --flow <flow> --step <step>`. If `hit:false` **or**
   `stale:true` → find it the normal goal-style way (below) and skip the rest of this list.
2. On a usable hit, with **ONE `browser_evaluate`**: first confirm the page URL matches the cached
   `page_url_pattern` (wrong page → treat as a miss). Resolve the cached `query`; if it matches
   **0 or more than 1** visible element, treat it as a **miss** (never pick one of several). Otherwise
   act, then **verify the action took**: a `type` step reads the field value back and confirms it
   equals what you typed; a `click` step confirms its expected next state (panel opened, field
   focused, step advanced) — "no error" is NOT success.
3. Verified → `python3 bin/ui_cache.py record …` (refresh liveness) and move on, **no snapshot taken**.
4. Any miss / ambiguous match / failed verify → `python3 bin/ui_cache.py invalidate --market <m>
   --flow <flow> --step <step>`, then **fall back to goal-style vision** (find it the human way).
   Once found and verified, **re-record** the freshly-found resolver:
   `python3 bin/ui_cache.py record --market <m> --flow <flow> --step <step> --strategy <css|aria|role|text>
   --query '<expr>' --action-kind <type|click> --url-pattern '<page-url-regex>'`. Prefer
   aria-label / role / name / visible-text — **never a hashed CSS class** (those churn every deploy).
   Always pass `--url-pattern`; a step with no page guard is always treated as stale.

A moved button is still re-found, not a break — page memory just remembers the new spot, and one slow
listing re-learns it after a redesign. Hard rules: the cache never bypasses the pacing/confirmation
rules — publish and send still go through `reply-pipeline.md` pacing + the recipe's confirm/anomaly
gates, and the pause hook still blocks every mutation. **Do NOT use `browser_evaluate` to submit the
final publish or send** — use it only to locate/fill fields and click non-final controls; the final
publish/send happens through the recipe's normal path after the gates. Stop-and-escalate on
logged-out/checkpoint as before — NEVER retry a cached selector in a loop (that's the anti-automation
tell). In `--dry-run`, log the `get`/`record` calls you would make; do not act.

## Tactics that work in practice (validated live on FB + Carousell; apply to every marketplace)
- **Photo upload:** set files directly on the hidden `input[type=file]` (Playwright
  `setInputFiles`) — don't fight the visible dropzone.
- **Overlay/focus-trap intercepts a click** ("…intercepts pointer events"): click via JS
  (`element.click()`) / a forced click to bypass the transparent overlay, instead of the
  normal pointer click.
- **Interstitials after an action:** dismiss post-publish tours and satisfaction surveys
  (e.g. Carousell's "Next/Okay" tour, then a feedback survey) before reading the result.
- **Multi-step composers:** some flows are 2 steps (e.g. FB: details → audience → Publish).
  Advance, then publish on the final step.
- **Capture the platform's price hint:** during the create flow, read the site's own
  suggested/sold price widget and return it — it's the strongest market anchor for the
  listing anomaly check (`listing.md` step 5b).
- **Auto mode:** these run without asking the seller to approve each action (tools allowlisted),
  but still under the pacing jitter + hourly cap. Stop-and-escalate on login/checkpoint.

## Identifiers

- `thread_id` — a stable handle for a conversation, **namespaced by marketplace**:
  `fb:<id>` / `carousell:<id>` (so the two inboxes never collide in `data/threads/`).
  If the site exposes a conversation URL or id, use it; otherwise derive a stable key
  from `buyer_handle + item_hint`. It's the dedupe key for the read-cursor.
- `msg_id` — a stable per-message id. If the site doesn't expose one, derive
  `"{ts}|{first-40-chars-of-text}"`. Must be stable across reads so the cursor works.

## Failure handling

If an action can't complete because you're **logged out / hit a checkpoint /
captcha / "confirm it's you"** screen: **stop that marketplace's pass and escalate to the
user** ("re-auth your <marketplace>") — and keep the other marketplaces + the seller channel
running. Do NOT retry in a tight loop — repeated failed attempts are exactly the
anti-automation signal that gets accounts flagged.

## Dry-run mode

When invoked with a `--dry-run` intent, do not actually click/type/send — instead
**log each action you *would* take** (`navigate(...)`, `type(...)`, `send()`).
This proves a command only uses this vocabulary, with zero real marketplace side effects.
Used by the build tests before any live run.
