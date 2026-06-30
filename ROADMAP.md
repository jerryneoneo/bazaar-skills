# Roadmap

Where SELLY Skills is headed. This is **directional, not a set of commitments**; order and timing
shift as we learn. The architecture is channel-, OS-, harness-, and model-agnostic by design (adapter
seams throughout), so most roadmap items slot in behind stable interfaces. The axes are at different
stages: the channel seam is clean (some adapters below are already built and just await onboarding
exposure), while the OS seam needs some core work beyond a new adapter (the daemon's instance lock and
notification path still assume macOS). For the system design behind these items, see
[ARCHITECTURE-OVERVIEW.md](ARCHITECTURE-OVERVIEW.md).

Versions track the root [`VERSION`](VERSION) file; shipped changes are recorded in
[CHANGELOG.md](CHANGELOG.md).

---

## ✅ Shipped: `v0.1.0`

The first public release. A personal agent that both **sells and buys** for you across informal P2P
marketplaces, running inside your own Claude Code session and driving your real logged-in Chrome.

- **Seller agent.** List from a few photos (vision ID + comps, then a public price and a private
  floor, then publish), auto-answer buyers from a learned Q&A bank, negotiate within your floor, and
  close.
- **Buyer agent.** Search your marketplaces, shortlist matches, negotiate within a secret max
  budget, and coordinate the handover (the faithful inverse of the seller side).
- **7 marketplaces.** Facebook Marketplace, Carousell, eBay, Mercari, OfferUp, Poshmark, Craigslist.
- **Channels.** Telegram and console.
- **OS.** macOS (always-on launchd daemon; interactive mode also works).
- **Harness.** Claude Code.
- **Autonomy presets.** `hands-free`, `balanced`, or `all-steps`, with above-list and above-budget
  decisions always escalated to you.
- **Pause & correct.** Stop the agent mid-flight and queue corrections it applies on resume.
- **Self-evaluation.** Offline checks that grade the agent's own behavior.

---

## ✅ Shipped: `v0.2.0`

Released 2026-06-28 (see [CHANGELOG.md](CHANGELOG.md)):

- **`/selly-catchup`.** One deep sweep across every listing, marketplace, and setup surface that
  reports everything waiting on you and offers to handle each item.
- **Instant wake mode.** Reply the moment a buyer messages (push-notification trigger), with polling
  as the safe default.
- **Turnkey macOS install.** `./setup` offers to install missing Node and Chrome via Homebrew.
- **Runtime health check.** `bin/healthcheck.py` confirms deps, Chrome/CDP, marketplace logins, and
  daemon state (read-only, no secrets).
- **Telegram "/" command menu.** Tappable everyday commands in the chat.
- **Leaner, cheaper daemon.** Fewer and cheaper background passes without losing responsiveness.

---

## 🔭 Later

Designed-for via adapter seams. Status varies by item, as noted.

- **More channels.** The **iMessage and WhatsApp adapters are already built and daemon-dispatched**;
  what remains is exposing them in onboarding (today onboarding offers only Telegram + console).
  **Slack** and running **multiple channels at once** are still planned (not yet built).
- **More operating systems.** Windows (Task Scheduler) has a real but incomplete platform adapter;
  Linux (`systemd`) has none yet. Both need some core work beyond a new adapter — the daemon's
  single-instance lock (`fcntl`), the notification trigger, and the health checks still assume macOS.
- **More harnesses and flexible model selection.** Additional agent harnesses beyond Claude Code
  (the Codex install layer exists; its runtime is an unverified stub), and per-task model choice
  through the same seam.
- **More marketplaces.** Depop, ThredUp, Nextdoor (catalogued today as stubs).
- **Discount-vs-new framing.** Show the real percentage off brand-new retail in listings and
  negotiation, from a seller-confirmed figure.

---

## ☁️ Dependent on the hosted rail

The **carousell.ai rail** is a separate hosted workstream and is **not yet wired in this build**.
When it integrates, two defaults light up (better experience, never mandatory):

- **carousell.ai publication.** A GEO-optimized, agent-discoverable storefront page kept in sync with
  your listing, for more reach and faster sales.
- **carousell.ai checkout.** Escrow with buyer protection, a tracked shipping label generated for
  you, and one clear total (price + delivery). Until then, the agent closes via **manual handover**.

---

## Out of scope (for now)

- Inventory management, or deciding *what* to sell.
- Anything that requires the agent to log in for you, hold your funds, or arrange offline meetups. It
  drives your existing logged-in session, and every deal ships P2P.

---

Have a suggestion? Open an issue or a PR. See [CONTRIBUTING.md](CONTRIBUTING.md).
