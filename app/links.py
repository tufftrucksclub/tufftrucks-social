"""Where a post links to. Pinterest has blocked tufftrucks.club, so pins can link to the product's Etsy
listing instead. Shopify products are matched to Etsy listings by cover type, vehicle and model years;
anything that doesn't match cleanly gets no link rather than a wrong one. Matches can be fixed by hand
on the Products page (saved in the "etsy_matches" setting)."""
from __future__ import annotations

import re

from . import db

PIN_LINK_MODES = {"store": "Your store (tufftrucks.club)", "etsy": "The matching Etsy listing", "none": "No link"}

_SYNONYMS = [(r"\bchevy\b", "chevrolet"), (r"\bsuper\s+duty\b", "superduty"), (r"\bf-?150\b", "f150"),
             (r"\blightning\b", "ev"), (r"\bd-ring\b", "dring"), (r"\bshackle mount\b", "shackle"),
             (r"\bjl\b|\bjk\b|\bjt\b", ""), (r"\bnot sport\b", ""), (r"\(2 pcs\)", "")]
# words that don't tell two listings apart
_NOISE = set("""compatible with and the for tow hook hooks covers cover flex fit snap snap-fit flex-fit personalized
uv uv-r resistant uv-resistant rounded full size coverage standard modular bumpers bumper ford gmc chevrolet ram jeep
tesla hyundai rivian 1500 pcs visor extension""".split())


def _kind(title: str) -> str:
    t = title.lower()
    if "personalized" in t:
        return "personalized"
    if "flex" in t:
        return "flex"
    if "snap" in t:
        return "snap"
    return re.sub(r"\W+", " ", t.split(" compatible")[0]).strip()  # accessories: their own name


def _years(title: str) -> tuple[int, int] | None:
    m = re.search(r"\b(20\d\d)\s*(?:-|–)\s*(20\d\d)\b", title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(20\d\d)\s*\+", title) or re.search(r"\((20\d\d)\+\)", title)
    if m:
        return int(m.group(1)), 2100
    return None


def _models(title: str) -> frozenset[str]:
    t = title.lower()
    for pat, rep in _SYNONYMS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"\b20\d\d\b", " ", t)
    words = re.findall(r"[a-z0-9/]+", t)
    return frozenset(w for w in words if w not in _NOISE and len(w) > 1)


def _same(a: str, b: str) -> bool:
    if _kind(a) != _kind(b) or _models(a) != _models(b):
        return False
    ya, yb = _years(a), _years(b)
    return ya is None or yb is None or (ya[0] <= yb[1] and yb[0] <= ya[1])


def auto_match(product: dict, etsy: list[dict]) -> dict | None:
    hits = [e for e in etsy if _same(product["title"], e["title"])]
    return hits[0] if len(hits) == 1 else None   # two candidates = ambiguous, so no guess


def etsy_listings() -> list[dict]:
    return db.q("SELECT id, title, url FROM products WHERE source = 'etsy' AND status = 'active' ORDER BY title")


def etsy_match(product: dict | None, etsy: list[dict] | None = None) -> dict | None:
    """The Etsy listing for a product: itself if it's an Etsy listing, your pick, or a confident match."""
    if not product:
        return None
    if product.get("source") == "etsy":
        return product
    picks = db.get_setting("etsy_matches") or {}
    etsy = etsy if etsy is not None else etsy_listings()
    if product["id"] in picks:
        return next((e for e in etsy if e["id"] == picks[product["id"]]), None)  # "" = you chose no link
    return auto_match(product, etsy)


def pin_mode() -> str:
    v = db.get_setting("pinterest_links")
    if v is True:
        return "store"
    if v in (False, None):
        return "none"
    return v if v in PIN_LINK_MODES else "store"


def pin_link(post: dict) -> str:
    """What a pin for this post should link to under the current Pinterest setting."""
    mode = pin_mode()
    product = post.get("product")
    if mode == "store":
        return (product or {}).get("url") or ""
    if mode == "etsy":
        e = etsy_match(product)
        return (e or {}).get("url") or ""
    return ""
