# ONBOARDING flow

**Triggers:** after INTRO `setup`, the `/onboard` command, or a jump from `/selly-install` /
`/selly` into one of the named anchors below (re-runnable any time to edit — show the current value
as the default and accept "keep"). Writes `data/seller_config.json` and seeds availability.

Uses only the `channel.md` verbs. All money/address handling follows the trust rules below. The
`##`-anchored sections (`CHOOSE_INTERFACE`, `CHOOSE_MARKETPLACES`, `APPROVALS`, `BUYER_PROFILE`) are
the single source of truth for those steps — full onboarding runs them in order; `/selly` jumps
straight to one (incl. `WAKE_SPEED`, the optional Instant-mode speed upgrade). SELLY is one agent that can **sell and buy**; the seller fields below write
`data/seller_config.json`, and the optional `BUYER_PROFILE` step writes `data/buyer_config.json`.

```
say  "Let's get you set up. You can re-run /onboard (or open /selly) any time to change these."

run  CHOOSE_INTERFACE                                       # how you talk to me (probe & bind)
ask  "What currency do you sell in?"                        -> currency        (e.g. SGD)
ask  "Your region / timezone?"                              -> region, timezone
run  CHOOSE_MARKETPLACES                                    # region-filtered platform picker

say  "Your full PICKUP address, couriers need it to collect. Buyers NEVER see this; I only
      use it to calculate delivery distance."
ask  "Address (block/unit, street, postcode, area)?"       -> origin {line1, postcode, area}
      # lat/lng optional; if provided, enables distance-band fees, else area-name zones only

say  "Everything ships P2P, no meetups. Let's set delivery zones so buyers get an accurate total."
ask  "Use the default zone→fee table, or enter your own?" options=[default=Use default, custom=Customize]
  default -> seed shipping.zones with the standard near/mid/far + unserviceable bands
  custom  -> ask for each band's fee (and area/distance match) -> shipping.zones[]
ask  "Default delivery size surcharges OK (small 0 / medium / large / bulky)?" options=[yes, edit]

ask  "Connect a calendar so I can answer 'when will it ship?' accurately?"
     options=[connect=Connect calendar, manual=Set windows manually, skip=Skip for now]
  connect -> set availability.source = "calendar_mcp"
             say "Make sure your Google Calendar MCP is connected in this harness."
  manual  -> ask weekly windows (days + times) -> write data/availability.json ;
             set availability.source = "manual"
  skip    -> set availability.source = "skip" ;
             say "No problem, I'll keep timing answers vague until you set this."

run  APPROVALS                                              # autonomy level (both layers)
run  WAKE_SPEED                                             # optional: Instant (notifications) vs Standard (poll)

ask  "Want me to BUY for you too? I can search the marketplaces and negotiate on your behalf."
     options=[yes=Set up buying, skip=Just selling for now]
  yes  -> run BUYER_PROFILE                                 # writes data/buyer_config.json
  skip -> say "No problem, enable buying any time via /selly -> buying."

write data/seller_config.json   (schema below)
say  "✅ All set! Send /list with photos to sell, or /search to buy."
```

## CHOOSE_INTERFACE
Pick the chat interface and bind it ("probe & bind" — full contract in `skills/channel/adapters.md`).
```
for adapter in [console, telegram]:   # supported today; iMessage + WhatsApp adapters land later
    d = adapter.detect()                                   # cheap, read-only probe
    record d.available + d.evidence/hint
say  the detection summary (e.g. "✅ Telegram token + chat found · console always available")
# DEFAULT-SUGGEST TELEGRAM — it's the recommended channel (reach the agent from your phone, anywhere;
# the console only works while this terminal session is open).
ask  "Which interface should I use?"
     options=[telegram=Telegram (recommended), console=Console (this terminal only)]
     default=telegram   -> choice
if the chosen adapter is not already bound:
    run choice.connect()    # telegram -> the BotFather walkthrough in skills/channel/adapters.md
                            #             (teaches: create the bot, copy the token, paste it here);
                            # console  -> no-op
    (sets the token env var for install.py gen-settings to pick up; verifies one round-trip)
# BIND GATE (telegram only) — do NOT write the binding until it is proven live + chat-bound:
#   1. after the token is pasted: `python3 bin/telegram.py verify`
#        token_valid:false (exit 3) -> say the reason, re-ask for the token, repeat (never write).
#   2. token good but chat_bound:false (exit 1) -> tell them to open t.me/<bot_username> and tap
#        Start; loop `telegram.py poll` then `telegram.py verify` until exit 0 (chat captured).
#        If they can't /start now, offer console and bind Telegram later — never bind a null chat_id.
write seller_config.channel = { adapter: choice, bound_at: <today>, detail: {…non-secret ids…} }
     # (only reached once verify returns exit 0 for telegram; console binds immediately)

# SWITCHING IS EASY — never lock the user in, and make sure they KNOW it:
#  • Right after binding, TELL them: "You can change your interface anytime — just say 'switch to
#    Telegram' or open /selly -> interface."
#  • If they chose console, also nudge: "Want messages on your phone instead? Switch to Telegram
#    anytime (~2 min with a BotFather token) — say the word."
#  • MID-ONBOARDING: if at ANY later step the user says "use Telegram" / "switch to Telegram" (or
#    BotFather feels fiddly and they'd rather start on console), jump straight back to this anchor,
#    bind the new adapter via connect(), then resume where they left off. No restart, no penalty.
#  • If switching while a daemon is loaded: uninstall -> rewrite seller_config.channel -> reinstall
#    (see adapters.md).
say  "Bound <choice>. You can switch anytime — say 'switch to Telegram' or use /selly -> interface."
```

## CHOOSE_MARKETPLACES
Offer only the platforms relevant to the seller's region (per `skills/marketplaces.md`).
```
registry = load data/marketplaces.json
offered  = [ m for m in registry if m.status=="active"
             and (region in m.regions or "*" in m.regions)
             and fulfillment in m.fulfillment ]
ask  "Which marketplaces?" options=[<m.id>=<m.display_name> for m in offered]   (multi-select)
     -> for each chosen id: marketplaces[id] = { enabled:true, auth:"unknown", connector:<type> }
        for each unchosen offered id: marketplaces[id] = { enabled:false }
# Connect each enabled chrome_session platform on its REGIONAL site (SG seller → ebay.com.sg,
# carousell.sg — never the global .com). The regional host is the single source of truth in the
# registry's `domains` map, resolved by bin/resolve_domain.py (see skills/marketplaces.md).
for id in enabled where registry[id].connector.auth=="chrome_session":
    host = python3 bin/resolve_domain.py --market <id> --region <region>   # -> regional host
    marketplaces[id].site = host                                          # persist for listing/scan
    navigate("https://<host>/")                                           # open the regional site
    # EARN the status — don't trust a self-report. Probe the live page (read-only, ~0 tokens):
    probe = python3 bin/login_check.py market <id>   # logged_in(0) / logged_out(1) / unknown(3)
        logged_in  -> marketplaces[id].auth = "confirmed" ; say "✅ Logged in to <display_name>."
        logged_out -> marketplaces[id].auth = "needs_login"
                      say "Log in to <host> in the Chrome I just opened, then say 'done' and I'll
                           re-check. I never sign in for you."
                      on 'done' -> re-run login_check; loop until logged_in (or the seller skips).
        unknown    -> # probe couldn't tell (DOM drift / odd page) — fall back to the seller:
                      confirm "Logged in to <display_name> for your region (<host>) in your Chrome?"
                          yes -> marketplaces[id].auth = "confirmed"
                          no  -> marketplaces[id].auth = "needs_login" ; (same log-in nudge + re-check)
    # NEVER auto-log-in (account safety; chrome_session = the seller's real Chrome handles auth).
say  a note that listings are filtered per item by category at publish time
     (e.g. "I'll only send fashion to Poshmark.").
```

## APPROVALS
Set the **autonomy level**, which configures both layers at once (see `skills/selly-config.md` →
"Two layers of autonomy").
```
say  "How much should I do on my own? Hands-free lets me list, search, and handle chats without
      asking each time. I always check with you before accepting an above-list/bidding offer."
ask  "Autonomy level?" options=[hands_free=Hands-free, balanced=Balanced (recommended),
                                all_steps=Approve every step]   -> level
write config.approvals.preset = level ; expand config.approvals.steps from the preset table
run  python3 bin/install.py gen-settings --harness <harness> --autonomy <level>   # layer 2
say  a one-line summary of what runs automatically vs what will still ping you.
     # On hands-free this includes the buyer side: search, offer, and close-within-budget run
     # without a prompt; only an above-budget buy (like an above-list bid) ever stops to ask.
     # The `takeover` gate is a hard floor (never auto): I always ask before stepping into a chat
     # you started on your own (an inbox-sweep takeover), at every autonomy level.
```

## STYLE
Set **how I deal**: my voice with buyers/sellers and how firm I am on the sell side. Writes
`data/style.json` (the persona profile) via `bin/style.py` (single source of truth, validated). The
default profile reproduces today's behavior, so this step is optional. `skills/style.md` is the
rulebook; the hard invariants there (no number leak, never claim human, no em-dashes, cheeky not cruel)
always outrank the persona.
```
show current = `python3 bin/style.py show`
say  "How should I sound, and how hard should I hold the line? I always stay friendly and never leak
      your floor, no matter the persona."
ask  "Tone?"            options=[friendly, warm, neutral, terse]            -> voice.tone
ask  "Humor?"           options=[none, light, playful]                      -> voice.humor
ask  "With lowballers?" options=[polite, firm, cheeky]                      -> voice.lowball_response
ask  "Sell firmness?"   options=[soft, balanced=Balanced (recommended), firm, hardline]
                                                                            -> negotiation.sell_firmness
ask  "Anything else about your style? (free text, optional)"               -> voice.persona
ask  "Learn my style over time?" options=[suggest=Suggest, then I apply (recommended),
                                          auto=Apply confident changes, off=Don't learn] -> learning
for (field, value) in answers: run `python3 bin/style.py set --field <field> --value "<value>"`
     # firmness can also use `python3 bin/style.py set-firmness <level>` (sets the sell knobs).
run  `python3 bin/style.py validate`   # fail-fast; re-ask on any error
# Pending learning suggestions (from /pause steering + /selly-eval), if any:
pending = `python3 bin/style.py proposals`
if pending: for each, say {current -> proposed, rationale}; ask apply? -> `python3 bin/style.py apply --id <id>`
say  "✅ Style saved. I'll deal in your voice. Change it anytime with /selly -> style."
```

## WAKE_SPEED
How fast SELLY notices a new buyer message. OPTIONAL speed upgrade — Standard works out of the box,
so never block onboarding on it. Single source of truth for `/selly -> speed`; full mechanism in
`skills/selly-config.md` "Wake speed" (resolver + tab_park + fail-open poll fallback). macOS only.
```
say  "Two speeds (pick one, change anytime):
      ⚡ Instant — I reply the moment a buyer messages on Facebook or Instagram, often answering
         straight from the notification. (Needs Full Disk Access so I can read notifications.)
      🛡️ Standard — hands-off, I check your inboxes on a quick cycle. No extra permissions."
ask  "Turn on Instant?" options=[instant=Turn on Instant, standard=Keep Standard (default)]
  standard -> say "Done — Standard polling is on. Turn on Instant anytime via /selly -> speed."
  instant  ->
     # macOS only — if not macOS, say so and fall back to Standard.
     # 1) Full Disk Access (TCC is user-only: open the pane + guide, then detect). Report the exact
     #    binary to enable from `python3 bin/notify_setup.py status` (.python = the daemon's Python).
     run `python3 bin/notify_setup.py open-fda`
     say  "I opened System Settings → Privacy & Security → Full Disk Access. Turn it ON for
           <status.python>, then say 'done'."
     # 2) Chrome notification permission for the push-capable markets (Meta: FB/IG). Try the auto
     #    grant; if it reports anything other than 'granted' (blocked / no tab), GUIDE the manual
     #    grant: open the site, click the tune/lock icon left of the address bar → Notifications →
     #    Allow, for facebook.com (and instagram.com).
     run `python3 bin/notify_setup.py grant-chrome`
     # 3) verify + report (fail-soft — Instant is additive; Standard polling always backstops it).
     run `python3 bin/notify_setup.py status`
     if status.instant_ready -> say "⚡ Instant is on. Facebook/Instagram wake me the moment a buyer
                                      messages; Carousell stays on Standard polling."
     else                    -> say "Instant is armed. It switches on automatically the moment a real
                                      Facebook notification arrives; until then I use Standard polling
                                      so nothing is missed."
     # Keeping the FB tab backgrounded so its push keeps firing is automatic (bin/tab_park.py).
     # Carousell has no web-push, so it always uses Standard polling. Both paths are fail-open.
```

## BUYER_PROFILE
Set up **buying**: where buys arrive, how you pay, and which marketplaces to search. Writes
`data/buyer_config.json` (the buyer mirror of `seller_config.json`). Reuses `channel` / `currency` /
`region` / `timezone` from `seller_config.json` when it exists; asks for them (run `CHOOSE_INTERFACE`,
ask currency/region) if this is a buy-only setup.
```
reuse channel, currency, region, timezone from seller_config.json if present, else ask for them.
say  "Your delivery address — sellers only get it once a deal is agreed; I use it to estimate the
      delivered total and to set the search location."
ask  "Delivery address (block/unit, street, postcode, area)?"   -> delivery_area {line1,postcode,area}
ask  "How do you usually pay?" options=[paynow, bank_transfer, cod]  (multi-select) -> payment_methods
# which marketplaces to SEARCH — region-filtered, same registry + login model as selling.
registry = load data/marketplaces.json
offered  = [ m for m in registry if m.status=="active" and (region in m.regions or "*" in m.regions) ]
ask  "Which marketplaces should I search?" options=[<m.id>=<m.display_name> for m in offered]  (multi)
     -> chosen: marketplaces[id] = { enabled:true, auth:"unknown", connector:<type> } ; else enabled:false
for id in enabled where registry[id].connector.auth=="chrome_session":
     navigate the regional site (resolve_domain.py) ; probe `python3 bin/login_check.py market <id>`
     -> logged_in: auth="confirmed" · logged_out: auth="needs_login" (+ log-in nudge + re-check loop)
        · unknown: fall back to confirm "logged in?" -> set marketplaces[id].auth
     # NEVER auto-log-in (account safety; the buyer's real Chrome handles auth).
write data/buyer_config.json {
     channel, currency, region, timezone, marketplaces, delivery_area,
     fulfillment:"ship_only", payment_methods, availability:{source:"skip"}, onboarded_at:<today> }
say  "✅ Buying is set up. Send /search (or /buy) and tell me what you're after."
```
`config.json` keeps runtime/agent knobs (pacing, quiet hours, `checkout_disclosure`/`handover_disclosure`, `approvals`). This file
holds seller identity / channel binding / marketplaces / shipping:
```json
{
  "channel":      { "adapter": "telegram", "bound_at": "<today>", "detail": {} },
  "currency":     "SGD",
  "region":       "SG",
  "timezone":     "Asia/Singapore",
  "marketplaces": {
    "fb":        { "enabled": true,  "auth": "confirmed", "connector": "browser", "site": "www.facebook.com" },
    "carousell": { "enabled": true,  "auth": "confirmed", "connector": "browser", "site": "www.carousell.sg" }
  },
  "origin":       { "line1": "...", "postcode": "...", "area": "...", "lat": null, "lng": null },
  "fulfillment":  "ship_only",
  "shipping":     { "zones": [ /* see seller_config sample */ ], "size_surcharge": {} },
  "availability": { "source": "calendar_mcp" },
  "onboarded_at": "<today>"
}
```
(Legacy `marketplaces: ["fb","carousell"]` arrays + a top-level `logins` map are auto-upgraded on
load via the read-shim in `skills/marketplaces.md`.)

## Trust rules (enforced in this flow)
- **Exact address is private.** Stored in `origin` and read only by `bin/shipping.py` for the
  distance/zone calc. It is **never** put into a listing, a buyer reply, or any model prompt.
- **Buyer delivery address** lives in `buyer_config.delivery_area`, shared with a seller only at deal
  time (the buyer mirror of the `origin` rule). The **max budget** is never written to `buyer_config`
  — it lives only in `data/budgets/<want_id>.json`, read only by `bin/budget_gate.py`.
- **Ship-only.** No meetup fields are collected; there is no offline-transaction path.
- **No secrets in config.** Channel tokens stay in the harness env (`settings.local.json` /
  `.codex/.env`), never in `seller_config.json`; `channel.detail` holds only non-secret ids.
