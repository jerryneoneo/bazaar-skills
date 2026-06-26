#!/usr/bin/env python3
"""checkout.py — issue the carousell.ai checkout link at close (STUB + rail seam).

When a deal is finalised and the seller's close method is `checkout`, the agent calls
this to mint a secure checkout link to post to the buyer. The link is where payment +
delivery are handled end-to-end (escrow, tracked label, funds-release) — the reason a
seller *chooses* checkout over dealing manually. That rail is a separate, hosted
checkout/escrow service (not in this repo). So today the link is a MOCK and the
record is local — see the RAIL SEAM block in `_issue_link` for the exact swap-in point.

Trust boundary (same discipline as floor_gate.py): checkout re-validates the agreed price
against the seller's HIDDEN floor before issuing — a buggy/malicious caller cannot mint a
link below floor. The floor value NEVER appears in stdout, the record, or an error message.

Usage:
    python3 checkout.py issue --item ID --thread MARKET:ID --price 90
Output (stdout, JSON):
    {"status":"issued","sale_id":"...","checkout_url":"https://carousell.ai/checkout/...",
     "item_id":"...","thread_id":"...","price":90,"currency":"SGD","issued_at":"..."}

Exit codes: 0 ok · 2 bad input · 3 data missing/invalid · 4 price below floor (rejected).
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import floor_gate  # reuse load_floor_record — the floor stays here, never leaves

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHECKOUTS_DIR = DATA_DIR / "checkouts"
SELLER_CONFIG_PATH = DATA_DIR / "seller_config.json"

CHECKOUT_URL_BASE = "https://carousell.ai/checkout"


class FloorRejected(Exception):
    """Agreed price is below the hidden floor — refuse to issue (message carries no value)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _currency() -> str:
    if SELLER_CONFIG_PATH.exists():
        cfg = json.loads(SELLER_CONFIG_PATH.read_text())
        return cfg.get("currency", "SGD")
    return "SGD"


def make_sale_id(item_id: str, thread_id: str, price: float) -> str:
    """Deterministic id from the deal's identity, so re-issuing the same close is idempotent.

    Carries no secret — derived only from the buyer-visible (item, thread, agreed price).
    """
    seed = f"{item_id}|{thread_id}|{price}".encode()
    return hashlib.sha1(seed).hexdigest()[:12]  # nosec B324 — id only, not a security hash


def price_meets_floor(item_id: str, price: float) -> bool:
    """True iff `price` is at or above the hidden floor. Returns a bool only — never the floor."""
    rec = floor_gate.load_floor_record(item_id)
    return price >= rec["floor"]


def _issue_link(sale_id: str, item_id: str, thread_id: str, price: float, currency: str) -> dict:
    """Return the checkout artifact for this sale.

    ───────────────────────────── RAIL SEAM ─────────────────────────────
    Today this returns a MOCK link. To go live, replace the body below with a
    call into the hosted checkout/escrow rail (via MCP/API). Keep this exact contract:

        in : {sale_id, item_id, thread_id, price, currency}
        out: {"checkout_url": <real Stripe-backed escrow URL>, "status": "issued"}

    The real rail additionally provisions escrow + a tracked label and owns the
    funds-release window; none of that changes this function's signature or the
    caller in skills/channel/notifications.md (close → checkout).

    The live carousell.ai checkout page is also where agent involvement is formally
    reviewed/disclosed to the buyer (see skills/voice.md Rule 3); that page UI owns the
    review surface. In this repo the interim disclosure is the `config.checkout_disclosure`
    line posted alongside the link in notifications.md (close → checkout).
    ──────────────────────────────────────────────────────────────────────
    """
    return {"checkout_url": f"{CHECKOUT_URL_BASE}/{sale_id}", "status": "issued"}


def issue(item_id: str, thread_id: str, price: float) -> dict:
    """Mint (or return the already-minted) checkout record for a finalised deal.

    Idempotent + immutable: the deterministic sale_id maps to one record; once written it
    is returned verbatim on re-issue (we never mutate an issued record).
    Raises FloorRejected if the agreed price is below floor.
    """
    if not price_meets_floor(item_id, price):
        raise FloorRejected("agreed price is not acceptable for this item")

    sale_id = make_sale_id(item_id, thread_id, price)
    path = CHECKOUTS_DIR / f"{sale_id}.json"
    if path.exists():
        return json.loads(path.read_text())  # already issued — return unchanged

    link = _issue_link(sale_id, item_id, thread_id, price, _currency())
    record = {
        "status": link["status"],
        "sale_id": sale_id,
        "checkout_url": link["checkout_url"],
        "item_id": item_id,
        "thread_id": thread_id,
        "price": price,
        "currency": _currency(),
        "issued_at": _now(),
    }
    CHECKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="checkout.py", add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_issue = sub.add_parser("issue", help="issue a checkout link for a finalised deal")
    p_issue.add_argument("--item", required=True)
    p_issue.add_argument("--thread", required=True)
    p_issue.add_argument("--price", required=True)
    args = parser.parse_args(argv[1:])

    item_id = args.item.strip()
    thread_id = args.thread.strip()
    if not item_id:
        raise ValueError("item is empty")
    if not thread_id:
        raise ValueError("thread is empty")
    try:
        price = float(args.price)
    except ValueError as exc:
        raise ValueError(f"price must be a number: {exc}") from exc
    if price <= 0:
        raise ValueError("price must be positive")
    # Normalize whole-dollar prices to int for clean output.
    return item_id, thread_id, (int(price) if price == int(price) else price)


def main(argv) -> int:
    try:
        item_id, thread_id, price = _parse_args(argv)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        result = issue(item_id, thread_id, price)
    except FloorRejected as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 4
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
