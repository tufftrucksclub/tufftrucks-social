"""Configuration: secrets come from .env, tunable behavior lives in the database
(editable on the Settings page)."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TTS_DATA_DIR", ROOT / "data"))
MEDIA_DIR = DATA_DIR / "media"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "social.db"

for d in (DATA_DIR, MEDIA_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


_load_dotenv(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ---------------------------------------------------------------- secrets / env
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")

SHOPIFY_STORE = env("SHOPIFY_STORE")  # e.g. tuff-trucks-club.myshopify.com
SHOPIFY_CLIENT_ID = env("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET")
SHOPIFY_ACCESS_TOKEN = env("SHOPIFY_ACCESS_TOKEN")  # legacy shpat_ token (optional)
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", "2026-07")
STORE_URL = env("STORE_URL", "https://tufftrucks.club")

ETSY_KEYSTRING = env("ETSY_KEYSTRING")
ETSY_SHARED_SECRET = env("ETSY_SHARED_SECRET")
ETSY_SHOP_ID = env("ETSY_SHOP_ID")
ETSY_SHOP_URL = env("ETSY_SHOP_URL")

META_PAGE_ID = env("META_PAGE_ID")
META_IG_USER_ID = env("META_IG_USER_ID")
META_ACCESS_TOKEN = env("META_ACCESS_TOKEN")
META_GRAPH_VERSION = env("META_GRAPH_VERSION", "v23.0")

TIKTOK_CLIENT_KEY = env("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = env("TIKTOK_CLIENT_SECRET")

PINTEREST_APP_ID = env("PINTEREST_APP_ID")
PINTEREST_APP_SECRET = env("PINTEREST_APP_SECRET")
PINTEREST_USE_SANDBOX = env("PINTEREST_USE_SANDBOX", "0") == "1"

X_CLIENT_ID = env("X_CLIENT_ID")
X_CLIENT_SECRET = env("X_CLIENT_SECRET")

# Where this app is reachable for OAuth callbacks (the browser you click
# "Connect" in must be able to open it).
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8420").rstrip("/")

DEMO_MODE = env("DEMO_MODE", "0") == "1"
PORT = int(env("PORT", "8420"))
# 0.0.0.0 accepts connections from the network; the Windows start scripts set 127.0.0.1 so only
# this computer (and Tailscale's https address, which proxies from it) can connect.
HOST = env("HOST", "0.0.0.0")

PLATFORMS = ["instagram", "facebook", "tiktok", "pinterest", "x"]
PLATFORM_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "tiktok": "TikTok",
    "pinterest": "Pinterest",
    "x": "X",
}

POST_KINDS = {
    "product_spotlight": "Product spotlight",
    "review": "Customer review",
    "review_photo": "Review photo + backdrop",
    "promo": "Promo / sale",
    "behind_scenes": "Behind the scenes",
    "custom": "Custom",
}

# ------------------------------------------------- defaults for DB settings
DEFAULT_SETTINGS: dict = {
    # Safety
    "dry_run": True,              # simulate publishing until you flip this off
    "posting_paused": False,      # big red stop button
    "hold_hours": 12,             # auto-approved posts sit in the queue this long before they can go out
    # What may publish without you
    "auto_approve_kinds": ["product_spotlight"],
    # Platforms
    "enabled_platforms": ["instagram", "facebook", "tiktok", "pinterest", "x"],
    "tiktok_mode": "inbox",       # inbox = drafts you finish in the app; direct = auto-post (needs TikTok audit)
    "x_allow_links": False,       # links on X cost ~13x more per post
    "pinterest_board_id": "",
    # Cadence
    "posts_per_week": 5,
    "review_posts_per_week": 2,
    "posting_times": {"mon": "18:30", "tue": "12:15", "wed": "18:30", "thu": "12:15",
                      "fri": "17:45", "sat": "10:30", "sun": "19:00"},
    "planning_day": "sun",
    "planning_time": "16:00",
    "min_review_rating": 5,
    # Brand
    "brand_name": "Tuff Trucks Club",
    "brand_voice": (
        "Family-run, made-to-order truck shop. Friendly, proud, a little playful. "
        "Truck-guy language without being crude. We started because we couldn't find "
        "blue tow hooks for our own truck, so we designed our own; everything we make "
        "comes from customer requests. Short punchy sentences. Emojis sparingly (max 2)."
    ),
    "brand_hashtags": ["#TuffTrucksClub", "#TowHookCovers", "#TruckMods"],
    "avoid": "No price promises unless the product data shows them. No claims about fit "
             "beyond the vehicles listed. Never name or tag customers.",
    "accent_color": "#1E88E5",
    "accent_color_2": "#FF3D7F",
    "watermark": "TUFFTRUCKS.CLUB",
    "timezone": "America/New_York",
}


def dumps(v) -> str:
    return json.dumps(v)
