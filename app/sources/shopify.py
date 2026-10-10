"""Shopify Admin GraphQL: pull products/sales and host post images on Shopify Files.

Auth: a Dev Dashboard app (client ID + secret, client-credentials grant, 24h tokens
refreshed automatically) or, if you still have one, a legacy custom-app shpat_ token.
Scopes needed: read_products, read_files, write_files (+ read_orders for best-sellers).
"""
from __future__ import annotations

import json
import mimetypes
import time
from datetime import timedelta
from pathlib import Path

import httpx

from .. import config, db


class ShopifyError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.SHOPIFY_STORE and (config.SHOPIFY_ACCESS_TOKEN or
                                          (config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET)))


def _token() -> str:
    if config.SHOPIFY_ACCESS_TOKEN:
        return config.SHOPIFY_ACCESS_TOKEN
    tok = db.get_token("shopify")
    if tok and db.parse_iso(tok.get("expires_at")) and db.parse_iso(tok["expires_at"]) > db.utcnow() + timedelta(minutes=10):
        return tok["access_token"]
    r = httpx.post(
        f"https://{config.SHOPIFY_STORE}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": config.SHOPIFY_CLIENT_ID,
              "client_secret": config.SHOPIFY_CLIENT_SECRET},
        timeout=30,
    )
    if r.status_code != 200:
        raise ShopifyError(f"Shopify token request failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    expires = db.utcnow() + timedelta(seconds=int(data.get("expires_in", 86399)))
    db.save_token("shopify", {"access_token": data["access_token"], "scope": data.get("scope", ""),
                              "expires_at": db.iso(expires)})
    return data["access_token"]


def gql(query: str, variables: dict | None = None) -> dict:
    url = f"https://{config.SHOPIFY_STORE}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    for attempt in range(4):
        r = httpx.post(url, json={"query": query, "variables": variables or {}},
                       headers={"X-Shopify-Access-Token": _token()}, timeout=60)
        if r.status_code == 429 or (r.status_code == 200 and "THROTTLED" in r.text):
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 401 and not config.SHOPIFY_ACCESS_TOKEN:
            db.delete_token("shopify")
            continue
        if r.status_code != 200:
            raise ShopifyError(f"Shopify API {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("errors"):
            raise ShopifyError(f"Shopify GraphQL error: {body['errors']}")
        return body["data"]
    raise ShopifyError("Shopify API kept throttling; try again later")


PRODUCTS_Q = """
query Products($cursor: String) {
  products(first: 50, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle productType tags status createdAt updatedAt onlineStoreUrl
      description(truncateAt: 700)
      priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount } }
      media(first: 8) { nodes { ... on MediaImage { image { url } } } }
    }
  }
}"""


def sync_products() -> int:
    if not configured():
        return 0
    cursor, n = None, 0
    while True:
        data = gql(PRODUCTS_Q, {"cursor": cursor})["products"]
        for p in data["nodes"]:
            images = [m["image"]["url"] for m in p["media"]["nodes"] if m and m.get("image")]
            pr = p.get("priceRangeV2") or {}
            lo = (pr.get("minVariantPrice") or {}).get("amount")
            hi = (pr.get("maxVariantPrice") or {}).get("amount")
            price = None
            if lo:
                price = f"${float(lo):.2f}" + (f"–${float(hi):.2f}" if hi and hi != lo else "")
            url = p.get("onlineStoreUrl") or f"{config.STORE_URL}/products/{p['handle']}"
            pid = f"shopify:{p['id']}"
            db.run(
                """INSERT INTO products(id, source, title, handle, url, description, product_type, tags,
                                        price, images, status, created_at, updated_at)
                   VALUES(?, 'shopify', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, handle=excluded.handle, url=excluded.url,
                     description=excluded.description, product_type=excluded.product_type, tags=excluded.tags,
                     price=excluded.price, images=excluded.images, status=excluded.status,
                     updated_at=excluded.updated_at""",
                (pid, p["title"], p["handle"], url, p.get("description") or "", p.get("productType") or "",
                 json.dumps(p.get("tags") or []), price, json.dumps(images), p["status"].lower(), p["createdAt"], p["updatedAt"]),
            )
            n += 1
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    db.log(f"Shopify: synced {n} active products")
    return n


BEST_SELLERS_Q = """
query Orders($q: String!) {
  orders(first: 100, query: $q, sortKey: CREATED_AT, reverse: true) {
    nodes { lineItems(first: 20) { nodes { quantity product { id } } } }
  }
}"""


def best_sellers(days: int = 30) -> dict[str, int]:
    """product id -> units sold in the last N days. Empty if read_orders isn't granted."""
    if not configured():
        return {}
    since = (db.utcnow() - timedelta(days=days)).date().isoformat()
    try:
        data = gql(BEST_SELLERS_Q, {"q": f"created_at:>={since}"})
    except ShopifyError as e:
        db.log(f"Shopify best-sellers skipped: {e}", "warn")
        return {}
    counts: dict[str, int] = {}
    for o in data["orders"]["nodes"]:
        for li in o["lineItems"]["nodes"]:
            if li.get("product"):
                key = f"shopify:{li['product']['id']}"
                counts[key] = counts.get(key, 0) + int(li["quantity"])
    return counts


# ------------------------------------------------------------ media hosting
STAGED_Q = """
mutation Staged($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""

FILE_CREATE_Q = """
mutation FileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files { id fileStatus ... on MediaImage { image { url } } }
    userErrors { field message }
  }
}"""

FILE_POLL_Q = """
query FilePoll($id: ID!) {
  node(id: $id) { ... on MediaImage { fileStatus image { url } } }
}"""


def upload_image(path: Path, alt: str = "") -> str:
    """Upload a local image to Shopify Files and return its public CDN URL."""
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = gql(STAGED_Q, {"input": [{
        "filename": path.name, "mimeType": mime, "resource": "IMAGE",
        "httpMethod": "POST", "fileSize": str(path.stat().st_size)}]})["stagedUploadsCreate"]
    if data["userErrors"]:
        raise ShopifyError(f"stagedUploadsCreate: {data['userErrors']}")
    target = data["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in target["parameters"]}
    with path.open("rb") as fh:
        r = httpx.post(target["url"], data=form, files={"file": (path.name, fh, mime)}, timeout=120)
    if r.status_code not in (200, 201, 204):
        raise ShopifyError(f"Staged upload failed ({r.status_code}): {r.text[:300]}")
    created = gql(FILE_CREATE_Q, {"files": [{"originalSource": target["resourceUrl"],
                                              "contentType": "IMAGE", "alt": alt[:500]}]})["fileCreate"]
    if created["userErrors"]:
        raise ShopifyError(f"fileCreate: {created['userErrors']}")
    f = created["files"][0]
    for _ in range(30):
        if f.get("image") and f["image"].get("url") and f.get("fileStatus") == "READY":
            return f["image"]["url"]
        if f.get("fileStatus") == "FAILED":
            raise ShopifyError("Shopify could not process the uploaded image")
        time.sleep(2)
        f = gql(FILE_POLL_Q, {"id": f["id"]})["node"] | {"id": f["id"]}
    raise ShopifyError("Timed out waiting for Shopify to process the image")
