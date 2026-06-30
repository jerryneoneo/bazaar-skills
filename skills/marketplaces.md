# Marketplace registry — region & category aware platform catalog

The catalog of marketplaces SELLY can list to lives in **`data/marketplaces.json`** (static,
source-controlled reference data — the analogue of the shipping zone table). It is read at
**onboarding** (filter offered platforms by the seller's region), **publish** (filter target
platforms by the item's category), and for **region→domain resolution** (which regional site to
connect to / post on — see Consumption 4). The seller's *selection* lives separately in
`seller_config.json → marketplaces`.

This is the marketplace analogue of `skills/channel/adapters.md` (which catalogs chat adapters).

## Registry entry schema (`data/marketplaces.json`)

```json
{
  "id": "poshmark",
  "display_name": "Poshmark",
  "regions": ["US", "CA", "AU"],          // ISO-ish codes, or ["*"] for global
  "categories": ["fashion", "apparel"],   // taxonomy tags, or ["*"] for any category
  "fulfillment": ["ship_only"],           // which seller_config.fulfillment values it supports
  "listing_flow": "skills/listing-flows/poshmark.md",  // recipe path, or null while a stub
  "listing_url": { "host": "poshmark.", "path": "/listing/" },  // URL-verify suffix fallback
  "domains": { "US": "poshmark.com", "CA": "poshmark.ca", "AU": "poshmark.com.au" },  // region → host
  "connector": { "type": "browser", "auth": "chrome_session" },
  "status": "active"                      // "active" = offered + publishable; "stub" = catalog only
}
```

- `regions` — `region in regions` OR `"*" in regions` means "offer here".
- `categories` — `"*"` matches any item; otherwise the item's `category_tag` must be in the list.
- `domains` — region code → full host of that marketplace's **regional site** (SG seller →
  `www.ebay.com.sg`, never global `ebay.com`). Resolution rule (`bin/resolve_domain.py`):
  `domains[region]` → else `domains["*"]` → else the `listing_url.host` suffix. Markets with no
  `domains` map (e.g. craigslist, whose host is metro-derived) keep the suffix fallback.
- `connector.type` ∈ `browser` | `api` | `mcp`; `connector.auth` ∈ `chrome_session` | `api_key` | `none`.
- `status` — `stub` entries are never offered at onboarding and refuse to publish (they exist so
  the catalog documents what is coming and a future flow can flip them to `active`).

## Category taxonomy (fixed)

`item.category_tag` is one of:

```
electronics · fashion · apparel · shoes · accessories · beauty · home_decor ·
furniture · toys · books · sports · collectibles · other
```

`skills/channel/listing.md`'s identify/vision step emits both the free-text `item.category`
(used by each marketplace's category picker) **and** a `category_tag` from this list (used by the
publish filter below). When unsure, use `other` — it only matches `"*"` platforms.

## Consumption 1 — onboarding (region filter)

In `skills/channel/onboarding.md` (`CHOOSE_MARKETPLACES` anchor):

```
registry = load data/marketplaces.json
offered  = [ m for m in registry if m.status == "active"
             and (seller_config.region in m.regions or "*" in m.regions)
             and seller_config.fulfillment in m.fulfillment ]
ask "Which marketplaces?" options=[<m.id>=<m.display_name> for m in offered]  (multi-select)
```

An SG / `ship_only` seller is offered FB + Carousell + eBay; a US seller is offered
FB + eBay + Mercari + OfferUp + Poshmark + Craigslist.

## Consumption 2 — publish (category filter)

In `skills/channel/listing.md` PUBLISH, before the per-platform loop:

```
eligible = [ id for id, sel in seller_config.marketplaces.items()
             if sel.enabled
             and registry[id].status == "active"
             and (item.category_tag in registry[id].categories or "*" in registry[id].categories) ]
```

Publish only to `eligible`. If a seller-enabled platform is excluded for this item, `say` it once,
e.g. *"Poshmark is fashion-only, so this desk goes to FB + eBay only."* This is what enforces
"no furniture to Poshmark" — at publish time, per item, not at onboarding. If `eligible` is empty,
`say` why and skip publishing (do not error).

## Consumption 3 — distribution (detect / cross-list / recommend)

`skills/channel/distribution.md` needs the same region/category match rule, but generalized into
three sets (where an item already lives, where it could be cross-listed, what new platforms suit it).
That logic lives in **`bin/distribution.py`** — the single source of the match rule, so the inline
filters above and the distribution flow never drift:

```
python3 bin/distribution.py --item <item_id>
→ { already_listed[], cross_list_candidates[], recommend_setup[{id,display_name,status}], dropped_enabled[{id,reason}] }
```

- `already_listed` — registry ids already in `item.listing_urls` (the dedupe anchor).
- `cross_list_candidates` — `enabled ∧ active ∧ category-match ∧ not already_listed` (publish now).
- `recommend_setup` — `not enabled ∧ region-match ∧ category-match ∧ not already_listed`; `active`
  ones are offered for setup + cross-list, `stub` ones are only mentioned as upcoming (never posted to).

The helper applies the same array→object read-shim below and reads only reference data + the
buyer-safe item record — never a floor or an address.

## Consumption 4 — region → domain (connect, publish, verify)

The `domains` map answers "*which regional site?*" — the seller's `region` picks the host so the
agent connects to and posts on the right country site. **`bin/resolve_domain.py`** is the single
source of the resolution rule:

```
python3 bin/resolve_domain.py --market ebay --region SG  → {"host": "www.ebay.com.sg"}
```

Consumed at three points:

- **Connect** (`onboarding.md` `CHOOSE_MARKETPLACES`): resolve the host, `navigate` Chrome there,
  and confirm login *on the regional site*; persist it as `seller_config.marketplaces[id].site`.
  If not logged in → already navigated there; `say` "log in to `<host>`" (never auto-log-in).
- **Publish** (`skills/listing-flows/*.md` step 1): `navigate("https://<host>/<flow path>")` using
  the resolved/persisted `site`, instead of a relative path that lands wherever Chrome happens to be.
- **Verify** (`bin/verify_listing_url.py --region <r>`): the recorded URL's host must match the
  region-specific domain, so a listing that landed on the wrong regional site (SG seller, but
  `ebay.com`) **fails closed**. Without `--region` it's the legacy host-suffix check (back-compat).

## `seller_config.marketplaces` — selection shape & migration

The selection evolves from a **flat array** to an **object keyed by id** (and absorbs the old
top-level `logins` map):

```json
"marketplaces": {
  "fb":        { "enabled": true,  "auth": "confirmed", "connector": "browser", "site": "www.facebook.com" },
  "carousell": { "enabled": true,  "auth": "confirmed", "connector": "browser", "site": "www.carousell.sg" },
  "ebay":      { "enabled": false }
}
```

- `enabled` — seller turned this platform on.
- `auth` — login state for this platform (`confirmed` | `unknown` | `needs_login`); replaces the
  old top-level `seller_config.logins`.
- `connector` — mirrors the registry `connector.type` for quick reads.
- `site` — the resolved regional host (Consumption 4) the seller is connected to / posts on,
  cached at connect time. Absent on legacy configs → re-resolve via `bin/resolve_domain.py`.

**Read-shim (back-compat — apply on every load):** if `seller_config.marketplaces` is an *array*,
treat each string `x` as
`{ x: { enabled: true, auth: (seller_config.logins[x] or "unknown"), connector: registry[x].connector.type } }`.
Existing deployments keep running untouched until the seller next runs onboarding or `/selly`,
which writes the new object shape. Iterate selections as
`for id, sel in marketplaces.items() if sel.enabled` everywhere (`sell-run.md`, `sell-watch.md`,
`listing.md`).

## Trust rules

- The registry is reference data only — it carries no seller secrets and no per-item floor/address.
- `connector.auth = chrome_session` means "the seller's real logged-in Chrome handles auth" — no
  marketplace password is ever stored by SELLY.
