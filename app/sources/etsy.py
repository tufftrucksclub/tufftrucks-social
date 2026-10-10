"""Etsy Open API v3 — read-only: shop reviews (with buyer photos when shared) and
active listings. Only an API key is needed (no OAuth): x-api-key = keystring:sharedsecret.
Buyer names are never returned by the API, and photos may be absent depending on
the buyer's privacy settings."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx

from .. import config, db

BASE = "https://api.etsy.com/v3/application"


class EtsyError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.ETSY_KEYSTRING and config.ETSY_SHOP_ID)


def _headers() -> dict:
    key = config.ETSY_KEYSTRING
    if config.ETSY_SHARED_SECRET and ":" not in key:
        key = f"{key}:{config.ETSY_SHARED_SECRET}"
    return {"x-api-key": key, "Accept": "application/json"}


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        r = httpx.get(f"{BASE}{path}", params=params or {}, headers=_headers(), timeout=45)
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise EtsyError(f"Etsy {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()
    raise EtsyError("Etsy rate limit; try again later")


def _ts(epoch) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def sync_listings() -> int:
    if not configured():
        return 0
    n, offset = 0, 0
    while True:
        data = _get(f"/shops/{config.ETSY_SHOP_ID}/listings/active",
                    {"limit": 100, "offset": offset, "includes": "Images"})
        for l in data.get("results", []):
            imgs = [i.get("url_fullxfull") for i in (l.get("images") or []) if i.get("url_fullxfull")]
            price = l.get("price") or {}
            price_s = None
            if price.get("amount") is not None and price.get("divisor"):
                price_s = f"${price['amount'] / price['divisor']:.2f}"
            db.run(
                """INSERT INTO products(id, source, title, url, description, tags, price, images, status,
                                        created_at, updated_at)
                   VALUES(?, 'etsy', ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, url=excluded.url,
                     description=excluded.description, tags=excluded.tags, price=excluded.price,
                     images=excluded.images, updated_at=excluded.updated_at""",
                (f"etsy:{l['listing_id']}", l.get("title", ""), l.get("url"),
                 (l.get("description") or "")[:1500], json.dumps(l.get("tags") or []), price_s,
                 json.dumps(imgs), _ts(l.get("created_timestamp")), _ts(l.get("updated_timestamp"))),
            )
            n += 1
        offset += 100
        if offset >= int(data.get("count", 0)):
            break
    db.log(f"Etsy: synced {n} active listings")
    return n


def _listing_title(listing_id) -> str:
    row = db.q1("SELECT title FROM products WHERE id = ?", (f"etsy:{listing_id}",))
    if row:
        return row["title"]
    try:
        return _get(f"/listings/{listing_id}").get("title", "")
    except EtsyError:
        return ""


def sync_reviews(max_pages: int = 5) -> int:
    """Pull newest reviews. Stops paging once it reaches reviews it already has."""
    if not configured():
        return 0
    new, offset = 0, 0
    for _ in range(max_pages):
        data = _get(f"/shops/{config.ETSY_SHOP_ID}/reviews", {"limit": 100, "offset": offset})
        results = data.get("results", [])
        seen_existing = False
        for rv in results:
            rid = f"etsy:{rv.get('transaction_id') or rv.get('listing_id')}-{rv.get('create_timestamp')}"
            if db.q1("SELECT 1 FROM reviews WHERE id = ?", (rid,)):
                seen_existing = True
                continue
            db.run(
                """INSERT INTO reviews(id, source, listing_id, product_title, rating, text, image_url, created_at)
                   VALUES(?, 'etsy', ?, ?, ?, ?, ?, ?)""",
                (rid, str(rv.get("listing_id") or ""), _listing_title(rv.get("listing_id")),
                 int(rv.get("rating") or 0), (rv.get("review") or "").strip(),
                 rv.get("image_url_fullxfull"), _ts(rv.get("create_timestamp") or rv.get("created_timestamp"))),
            )
            new += 1
        offset += 100
        if seen_existing or len(results) < 100:
            break
    if new:
        db.log(f"Etsy: {new} new reviews")
    return new
