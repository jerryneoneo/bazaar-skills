# Listing flow — Carousell.ai (MCP-mode)

Per-site recipe for publishing one item to **carousell.ai** (the first-party `bazaar` backend) through
its **MCP server**, instead of a browser. Called by `skills/channel/listing.md` after the seller
confirms publish — exactly like the browser recipes. The orchestrator is connector-agnostic: it just
`follow`s this file, then verifies and records the returned URL.

> This is the api/mcp connector the browser [carousell.md](carousell.md) recipe anticipates ("if a
> first-party Carousell API/MCP becomes available, it can replace this recipe behind the same step
> contract, no change to the listing flow"). The server is registered in `.mcp.json` as `local-bazaar`
> (`http://localhost:9282/mcp`); its tools are `mcp__local-bazaar__*`.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe). No photos / floor / address are sent —
the bazaar listing model is `title + description + price_cents + currency` only.

## Steps

1. **Build the listing arguments** (money converted in Python, never composed in the prompt):
   `python3 bin/bazaar_args.py --item <item_id>` → `{title, description, price_cents, currency}`.
   On non-zero exit (missing title/price), notify + **skip this market** — never guess the values.
2. **Read the seller's bazaar API key** from the pass environment:
   `python3 -c "import os;print(os.environ.get('BAZAAR_API_KEY',''))"`.
   If empty → carousell.ai isn't provisioned yet; notify once ("carousell.ai not connected") + skip.
3. **Create the listing over MCP.** Call the tool `mcp__local-bazaar__create_listing` with the merged
   object `{ "api_key": <key>, "title", "description", "price_cents", "currency" }` (step 2 + step 1).
   → returns `{ "id", "seller_id", "title", "price_cents", "currency", "status", ... }`.
   On tool error (auth failure, or connection refused = backend down) → notify + skip; no retry-loop.
4. **Derive the live URL from the authoritative id** the backend returned:
   read the base with
   `python3 -c "import json;print(json.load(open('data/carousell_ai.json'))['web_base_url'])"`
   (`http://localhost:3001`), then `url = <web_base_url>/listing/<id>`.
5. **Return `url`** to the caller. The caller hard-gates it with
   `python3 bin/verify_listing_url.py --market carousell-ai --url "<url>"` before recording — a garbled
   id or empty response fails closed there.

> **URL invariant (connector-specific).** The browser recipes must READ the permalink from the live DOM
> and never compose one. For this MCP connector the trust anchor is instead the **id returned by
> `create_listing`** — authoritative, from the backend that just created the row — and the derived URL
> is still verified by `verify_listing_url.py`. It is not a hallucinated or inferred link. If you want an
> extra check, confirm with `mcp__local-bazaar__get_listing { api_key, id }` before returning.

## Take-down recipe (sale → done / delist)
`mcp__local-bazaar__update_listing { api_key, id: <trailing path segment of
items.listing_urls["carousell-ai"]>, status: "sold" }` (or the backend's removed status). PATCH
semantics — only the fields you pass change.

## Guardrails
- **Fail closed; no secrets in the listing.** Never put the floor, an address, or the API key into the
  title/description. The key is only ever the `api_key` argument, read from the environment (step 2).
- Backend unreachable / tool error / empty id → skip this market; the caller lists the other eligible
  markets and reports carousell.ai as not-confirmed. No tight retry.
- `--dry-run`: log the step-1 args + the intended tool call; call nothing.
