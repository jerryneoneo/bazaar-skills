# INTRO flow

**Triggers:** the first ever `watch()` event from the seller, or an explicit `/start` command,
when `data/seller_config.json` does not yet exist (not onboarded).

Uses only the `channel.md` verbs — runs identically on any adapter.

The capabilities block below (the `CAPABILITIES` body) is the single source of "what Bazaar does";
`/bazaar` reuses it for the menu header, so keep it adapter- and marketplace-neutral.

```
# CAPABILITIES (reused by /bazaar)
say  "👋 I'm Bazaar, Carousell's agent skills for informal marketplaces, your P2P marketplace
      assistant — I help you sell AND buy.

      Here's what I do:
      • List your items across the marketplaces that fit your region (FB Marketplace, Carousell,
        eBay, Mercari, OfferUp, Poshmark, Craigslist, …) — only the ones that suit each item
      • Buy for you too: tell me what you want, I search your marketplaces, shortlist the best
        matches, then negotiate within your budget (your max stays secret) and coordinate the handover
      • Review your existing inbox: I spot chats you started on your own (e.g. you messaged a few
        sellers about an iPhone) and offer to take them over and negotiate them for you
      • Talk to you on Telegram or right here in the console (iMessage + WhatsApp coming soon)
      • Reply to buyers and sellers on your behalf, in a natural voice (agent involvement is disclosed for review at checkout, not announced in every chat; if asked outright I won't claim to be human)
      • Negotiate within limits you set: your lowest sell price / highest buy budget stays secret
      • Everything ships P2P (no offline meetups), so there's one clear total: price + delivery

      You choose how hands-off I am: I can run fully autonomously, or check with you at each step —
      either way I always confirm before accepting an above-list/bidding offer."

ask  "Ready to set up? Takes ~2 minutes." options=[setup=Set up now, later=Maybe later]
  setup -> hand off to ONBOARDING (skills/channel/onboarding.md)
  later -> say "No rush, send /onboard whenever you're ready."
```

Notes:
- On the Telegram adapter, first contact also captures the authorized `chat_id`
  (handled inside `telegram.py poll`).
- The disclosure model here matches the runtime: the agent does not announce itself in chat
  (see `skills/voice.md` Rule 3); agent involvement is disclosed for review at checkout (sell)
  or handover (buy), and it never claims to be human if asked outright.
