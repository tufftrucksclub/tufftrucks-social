"""The social media manager itself: sync → plan → draft → render → approve → publish → measure."""
from __future__ import annotations

import json
import random
import threading
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import brain, config, db, links
from .publishers import REGISTRY, PublishError
from .sources import etsy, photos, shopify
from .studio import backdrops, compose, video

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
REVIEW_KINDS = ("review", "review_photo")


def tz() -> ZoneInfo:
    return ZoneInfo(db.get_setting("timezone") or "America/New_York")


def local_now() -> datetime:
    return datetime.now(tz())


# ===================================================================== sync
def sync_sources() -> dict:
    out = {"shopify": 0, "etsy_listings": 0, "etsy_reviews": 0, "photos_rated": 0, "errors": []}
    for key, fn in (("shopify", shopify.sync_products), ("etsy_listings", etsy.sync_listings),
                    ("etsy_reviews", etsy.sync_reviews)):
        try:
            out[key] = fn()
        except Exception as e:
            out["errors"].append(f"{key}: {e}")
            db.log(f"Sync {key} failed: {e}", "error")
    try:
        out["photos_rated"] = rate_new_review_photos()
    except Exception as e:
        out["errors"].append(f"photo rating: {e}")
        db.log(f"Rating review photos failed: {e}", "error")
    lib = sync_photos()
    out["library_new"], out["library_rated"] = lib["library_new"], lib["library_rated"]
    out["errors"] += lib["errors"]
    return out


def sync_photos() -> dict:
    """Pull new photos from iCloud / folders, then have Claude rate them."""
    res = photos.scan()
    out = {"library_new": res["icloud"] + res["folders"], "library_rated": 0, "errors": res["errors"]}
    try:
        out["library_rated"] = rate_library_photos()
    except Exception as e:
        out["errors"].append(f"library rating: {e}")
        db.log(f"Rating library photos failed: {e}", "error")
    return out


def rate_library_photos(limit: int | None = None, batch: int = 6) -> int:
    if not brain.available():
        return 0
    limit = limit or int(db.get_setting("photos_rate_per_scan") or 36)
    styles = [k for k in backdrops.list_styles() if not k.startswith("ai:")]
    n, tried = 0, set()
    while len(tried) < limit:
        # Pick each batch fresh, so a photo you hide while this runs is never sent to Claude.
        rows = [r for r in db.q("SELECT * FROM photos WHERE score IS NULL AND hidden = 0 AND source != 'edit' "
                                "ORDER BY taken_at DESC LIMIT ?", (limit,)) if r["id"] not in tried]
        rows = rows[:min(batch, limit - len(tried))]
        if not rows:
            break
        tried.update(r["id"] for r in rows)
        chunk, imgs = [], []
        for r in rows:
            try:
                imgs.append(compose.load_image(str(config.DATA_DIR / r["local_path"])))
                chunk.append(r)
            except Exception as e:
                db.run("UPDATE photos SET score = 1, notes = ?, rated_at = ? WHERE id = ?",
                       (json.dumps({"description": f"Couldn't open: {e}"}), db.iso(), r["id"]))
        if not chunk:
            continue
        for r, res in zip(chunk, brain.rate_library_photos(imgs, styles)):
            if not res:
                continue
            res = dict(res)
            res.pop("index", None)
            # what the Studio should do with it: a dull background behind a clean subject gets replaced
            if res.get("background") == "mundane" and res.get("cutout_ok"):
                res["placement"] = "cutout"
            elif res.get("background") == "great":
                res["placement"] = "full"
            else:
                res["placement"] = "framed"
            if res.get("suggested_backdrop") not in styles:
                res["suggested_backdrop"] = "garage"
            # ...and one hidden during the Claude call itself isn't saved as rated
            # tags you added before it was rated stay, and Claude's suggestions join them
            mine = photos.tags_of(db.q1("SELECT tags FROM photos WHERE id = ?", (r["id"],)) or {})
            db.run("UPDATE photos SET score = ?, notes = ?, rated_at = ?, tags = ? WHERE id = ? AND hidden = 0",
                   (int(res.get("score") or 1), json.dumps(res), db.iso(),
                    json.dumps(photos.clean_tags(mine + photos.default_tags(res, r["source"]))), r["id"]))
            n += 1
    if n:
        db.log(f"Claude rated {n} library photo{'s' if n != 1 else ''}")
    return n


def library_picks(limit: int = 25, min_score: int = 6) -> list[dict]:
    """Best unused library photos, for the planner."""
    rows = db.q("SELECT * FROM photos WHERE hidden = 0 AND used_at IS NULL AND score >= ? "
                "ORDER BY score DESC, taken_at DESC LIMIT ?", (min_score, limit))
    for r in rows:
        r["n"] = db.jl(r.get("notes"), {}) or {}
        r["n"]["tags"] = photos.tags_of(r)
    return rows


def rate_new_review_photos(limit: int = 15) -> int:
    if not brain.available():
        return 0
    min_rating = int(db.get_setting("min_review_rating") or 5)
    rows = db.q("SELECT * FROM reviews WHERE image_url IS NOT NULL AND image_url != '' "
                "AND photo_score IS NULL AND rating >= ? ORDER BY created_at DESC LIMIT ?", (min_rating, limit))
    styles = list(backdrops.list_styles())
    n = 0
    for r in rows:
        try:
            img = compose.load_image(r["image_url"])
            res = brain.rate_review_photo(img, r["text"] or "", styles)
            notes = res.get("notes", "")
            if res.get("privacy_issue"):
                notes = "⚠ Possible plate/face/personal info visible. " + notes
            db.run("UPDATE reviews SET photo_score = ?, photo_notes = ? WHERE id = ?",
                   (int(res.get("score", 0)) if res.get("shows_product", True) else 1,
                    json.dumps({"notes": notes, "placement": res.get("placement"),
                                "backdrop": res.get("suggested_backdrop"),
                                "privacy_issue": bool(res.get("privacy_issue"))}), r["id"]))
            n += 1
        except Exception as e:
            db.log(f"Couldn't rate photo for review {r['id']}: {e}", "warn")
    return n


# ================================================================= planning
def next_week_start(today: date | None = None) -> date:
    today = today or local_now().date()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _slot(week_start: date, day: str) -> datetime:
    times = db.get_setting("posting_times") or {}
    hh, mm = (times.get(day) or "18:00").split(":")
    d = week_start + timedelta(days=DAY_KEYS.index(day))
    return datetime.combine(d, time(int(hh), int(mm)), tzinfo=tz())


def perf_summary() -> str:
    hint = db.get_setting("planner_hint") or ""
    top = db.q("""SELECT p.kind, p.backdrop, p.overlay_text, t.platform,
                  COALESCE(m.likes,0)+COALESCE(m.comments,0)+COALESCE(m.shares,0)+COALESCE(m.saves,0) AS eng
                  FROM post_targets t JOIN posts p ON p.id = t.post_id
                  JOIN metrics m ON m.id = (SELECT id FROM metrics WHERE target_id = t.id ORDER BY id DESC LIMIT 1)
                  WHERE t.published_at >= ? ORDER BY eng DESC LIMIT 8""",
               (db.iso(db.utcnow() - timedelta(days=42)),))
    lines = [f"- {r['platform']}: {r['kind']} '{r['overlay_text']}' on {r['backdrop']} → {r['eng']} engagements"
             for r in top]
    return (hint + "\nTop recent posts:\n" + "\n".join(lines)).strip() if (hint or lines) else ""


def plan_week(week_start: date | None = None, replace: bool = False) -> dict:
    """Ask Claude for next week's plan and build every post (captions + graphics)."""
    week_start = week_start or next_week_start()
    week_key = f"{week_start.isocalendar().year}-W{week_start.isocalendar().week:02d}"
    existing = db.q("SELECT id, status FROM posts WHERE week = ? AND created_by = 'planner'", (week_key,))
    if existing and not replace:
        return {"week": week_key, "created": 0, "note": "Week already planned"}
    if replace:
        for p in existing:
            if p["status"] in ("draft", "needs_approval", "approved", "rejected"):
                delete_post(p["id"])

    products = db.q("SELECT * FROM products WHERE status = 'active' AND images IS NOT NULL AND images != '[]' "
                    "ORDER BY COALESCE(last_featured_at, '0') ASC")
    min_rating = int(db.get_setting("min_review_rating") or 5)
    reviews = db.q("SELECT * FROM reviews WHERE used_at IS NULL AND hidden = 0 AND rating >= ? "
                   "AND text IS NOT NULL AND text != '' ORDER BY COALESCE(photo_score, 0) DESC, created_at DESC "
                   "LIMIT 40", (min_rating,))
    if not products:
        return {"week": week_key, "created": 0, "note": "No products yet — run a sync first"}

    styles = [s for s in backdrops.list_styles()]
    lib = library_picks() if brain.available() else []
    if brain.available():
        try:
            best = shopify.best_sellers() if shopify.configured() else {}
        except Exception:
            best = {}
        plan = brain.plan_week(week_start, products[:120], reviews, best, perf_summary(), styles, lib)
    else:
        plan = _fallback_plan(products, reviews, styles)
        db.log("No ANTHROPIC_API_KEY — used the simple built-in planner", "warn")

    if plan.get("notes"):
        db.run("INSERT INTO reports(created_at, kind, body) VALUES(?, 'plan_notes', ?)",
               (db.iso(), json.dumps({"week": week_key, "notes": plan["notes"]})))

    enabled = set(db.get_setting("enabled_platforms") or config.PLATFORMS)
    product_ids = {p["id"] for p in products}
    review_ids = {r["id"] for r in reviews}
    photo_ids = {p["id"] for p in lib}
    used_photos: set[str] = set()
    created, used_days = 0, set()
    for item in plan.get("posts", []):
        day = item.get("day")
        if day not in DAY_KEYS or day in used_days:
            continue
        kind = item.get("kind") if item.get("kind") in config.POST_KINDS else "product_spotlight"
        pid = item.get("product_id") if item.get("product_id") in product_ids else None
        rid = item.get("review_id") if item.get("review_id") in review_ids else None
        phid = item.get("photo_id") if item.get("photo_id") in photo_ids - used_photos else None
        if kind in REVIEW_KINDS:
            phid = None
            if not rid:
                continue
        elif kind == "showcase":
            if not phid:
                continue
        elif not pid and not phid:
            continue
        sched = _slot(week_start, day)
        if sched <= local_now() + timedelta(hours=1):
            continue
        platforms = [p for p in (item.get("platforms") or config.PLATFORMS) if p in enabled] or list(enabled)
        post_id = create_post(kind=kind, product_id=pid, review_id=rid, angle=item.get("angle", ""),
                              headline=item.get("headline", ""), backdrop=item.get("backdrop", "garage"),
                              scheduled_at=sched, platforms=platforms, created_by="planner", week=week_key,
                              photo_id=phid, video_style=item.get("video_style"))
        extra = [i for i in (item.get("video_photo_ids") or []) if i in photo_ids and i != phid][:3]
        if extra:   # the planner's extra shots go after the first photos the video would pick anyway
            picks = list(dict.fromkeys(auto_video_choices(get_post(post_id))[:3] + ["lib:" + i for i in extra]))
            db.run("UPDATE posts SET video_photos = ? WHERE id = ?", (json.dumps(picks), post_id))
        used_days.add(day)
        if phid:
            used_photos.add(phid)
        try:
            build_post(post_id)
        except Exception as e:
            db.log(f"Building post #{post_id} failed: {e}", "error")
            db.run("UPDATE posts SET status='needs_approval', needs_human_reason=? WHERE id=?",
                   (f"Build failed: {e}", post_id))
        created += 1
    db.log(f"Planned {created} posts for {week_key}")
    return {"week": week_key, "created": created, "note": plan.get("notes", "")}


def _fallback_plan(products, reviews, styles) -> dict:
    """Used when no Claude API key is set (demo / offline)."""
    s = db.all_settings()
    n = int(s["posts_per_week"])
    n_rev = min(int(s["review_posts_per_week"]), len(reviews))
    days = [d for d in DAY_KEYS][:7]
    random.Random(len(products)).shuffle(days)
    days = sorted(days[:n], key=DAY_KEYS.index)
    posts, pi, ri = [], 0, 0
    builtin = [x for x in styles if x in backdrops.BUILTIN]
    for i, d in enumerate(days):
        style = builtin[i % len(builtin)]
        if ri < n_rev and i % 2 == 1:
            r = reviews[ri]; ri += 1
            posts.append({"day": d, "kind": "review_photo" if r.get("image_url") else "review",
                          "review_id": r["id"], "angle": "Customer love", "headline": "Straight from a customer",
                          "backdrop": style, "platforms": config.PLATFORMS})
        else:
            p = products[pi % len(products)]; pi += 1
            posts.append({"day": d, "kind": "product_spotlight", "product_id": p["id"],
                          "angle": "Spotlight", "headline": "Made to order for your ride",
                          "backdrop": style, "platforms": config.PLATFORMS})
    return {"notes": "Simple rotation (connect Claude for smarter plans).", "posts": posts}


# ================================================================ post build
def create_post(*, kind, product_id=None, review_id=None, angle="", headline="", backdrop="garage",
                scheduled_at: datetime | None = None, platforms=None, created_by="manual", week=None,
                source_image=None, photo_id=None, placement=None, video_style=None) -> int:
    now = db.iso()
    if photo_id:
        ph = photos.get(photo_id)
        if ph:
            placement = placement or ph["n"].get("placement")
            if backdrop == "garage" and ph["n"].get("suggested_backdrop"):
                backdrop = ph["n"]["suggested_backdrop"]
    post_id = db.run(
        """INSERT INTO posts(kind, title, angle, product_id, review_id, source_image, backdrop, overlay_text,
                             scheduled_at, status, created_by, week, created_at, updated_at, photo_id, placement,
                             video_style)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)""",
        (kind, "", angle, product_id, review_id, source_image, backdrop, headline,
         db.iso(scheduled_at) if scheduled_at else None, created_by, week, now, now, photo_id, placement,
         video_style if video_style in video.STYLES else None))
    for p in platforms or db.get_setting("enabled_platforms"):
        db.run("INSERT OR IGNORE INTO post_targets(post_id, platform, status) VALUES(?, ?, 'pending')", (post_id, p))
    return post_id


def get_post(post_id: int) -> dict | None:
    post = db.q1("SELECT * FROM posts WHERE id = ?", (post_id,))
    if not post:
        return None
    post["targets"] = db.q("SELECT * FROM post_targets WHERE post_id = ? ORDER BY "
                           "CASE platform WHEN 'instagram' THEN 1 WHEN 'facebook' THEN 2 WHEN 'tiktok' THEN 3 "
                           "WHEN 'pinterest' THEN 4 ELSE 5 END", (post_id,))
    post["product"] = db.q1("SELECT * FROM products WHERE id = ?", (post["product_id"],)) if post["product_id"] else None
    post["review"] = db.q1("SELECT * FROM reviews WHERE id = ?", (post["review_id"],)) if post["review_id"] else None
    post["photo"] = photos.get(post["photo_id"]) if post.get("photo_id") else None
    return post


def _layout(post, photo) -> str:
    if post["kind"] in REVIEW_KINDS:
        return "review_photo" if photo else "review_card"
    return "product"


def _pick_photo(post) -> str | None:
    if post.get("photo"):
        try:
            return photos.best_image(post["photo"]["id"])
        except Exception as e:
            db.log(f"Post #{post['id']}: library photo unavailable ({e})", "warn")
    if post.get("source_image"):
        return post["source_image"]
    if post["kind"] in REVIEW_KINDS and post.get("review") and post["review"].get("image_url"):
        if post["kind"] == "review_photo" or (post["review"].get("photo_score") or 0) >= 6:
            return post["review"]["image_url"]
        return None
    if post.get("product"):
        imgs = db.jl(post["product"]["images"])
        return imgs[0] if imgs else None
    return None


def _template_captions(post) -> dict:
    s = db.all_settings()
    tags = " ".join(s["brand_hashtags"])
    prod = post.get("product") or {}
    rv = post.get("review") or {}
    name = prod.get("title") or rv.get("product_title") or "our tow hook covers"
    link = prod.get("url") or config.STORE_URL
    if post["kind"] in REVIEW_KINDS and rv:
        body = f"★★★★★ “{rv['text'][:300]}”\n\nThanks for being part of the club! 🙌"
    else:
        body = f"{post.get('overlay_text') or 'Made to order'} — {name}. Built by our family, for your ride."
    return {"instagram": f"{body}\n\nLink in bio.\n\n{tags}", "facebook": f"{body}\n\n{link}",
            "tiktok": f"{body[:120]} {tags}", "pinterest_title": name[:90],
            "pinterest": f"{body[:400]}", "x": f"{body[:220]} {s['brand_hashtags'][0]}",
            "headline": post.get("overlay_text") or "Made to order", "subline": name[:60],
            "needs_human": False}


def build_post(post_id: int, redraft: bool = True) -> dict:
    """(Re)write captions with Claude, render every platform's graphic, and set approval state."""
    post = get_post(post_id)
    s = db.all_settings()
    if redraft:
        if brain.available():
            caps = brain.draft_captions(post, post.get("product"), post.get("review"),
                                        (post.get("photo") or {}).get("n"))
        else:
            caps = _template_captions(post)
        if caps.get("headline") and not post.get("overlay_text"):
            post["overlay_text"] = caps["headline"]
        db.run("UPDATE posts SET overlay_text = ?, title = ?, needs_human_reason = ?, updated_at = ? WHERE id = ?",
               (post["overlay_text"] or caps.get("headline", ""), caps.get("subline", ""),
                caps.get("needs_human_reason") if caps.get("needs_human") else None, db.iso(), post_id))
        link = (post.get("product") or {}).get("url") or config.STORE_URL
        for t in post["targets"]:
            cap = caps.get(t["platform"], "")
            db.run("UPDATE post_targets SET caption = ?, title = ?, link = ? WHERE id = ?",
                   (cap, caps.get("pinterest_title", "") if t["platform"] == "pinterest" else "",
                    link if t["platform"] in ("pinterest", "facebook") else "", t["id"]))
        post = get_post(post_id)
    else:
        caps = {"needs_human": bool(post.get("needs_human_reason"))}

    render_media(post)

    # ---- approval decision
    reason = post.get("needs_human_reason") if caps.get("needs_human") else None
    auto_kinds = set(s.get("auto_approve_kinds") or [])
    sched = db.parse_iso(post.get("scheduled_at"))
    hold = timedelta(hours=float(s.get("hold_hours") or 0))
    photo_flag = ""
    if post.get("review") and post["review"].get("photo_notes"):
        if (db.jl(post["review"]["photo_notes"], {}) or {}).get("privacy_issue"):
            photo_flag = "Customer photo may show a plate/face."
    if post.get("photo") and post["photo"]["n"].get("privacy_issue"):
        photo_flag = "Your photo may show a plate/face/personal info."
    # photos from your own library always get a human look before they go public
    auto = (post["kind"] in auto_kinds and post["kind"] not in REVIEW_KINDS and not reason and not photo_flag
            and not post.get("photo_id") and sched is not None and sched >= db.utcnow() + hold)
    status = "approved" if auto else "needs_approval"
    db.run("UPDATE posts SET status = ?, approval = ?, needs_human_reason = ?, updated_at = ? WHERE id = ?",
           (status, "auto" if auto else None, reason or photo_flag or None, db.iso(), post_id))
    return get_post(post_id)


def _still_spec(post: dict) -> dict:
    photo = _pick_photo(post)
    rv = post.get("review") or {}
    placement = post.get("placement") or "auto"
    if post["kind"] in REVIEW_KINDS and not post.get("placement"):
        placement = (db.jl(rv.get("photo_notes"), {}) or {}).get("placement") or "framed"
    return {"layout": _layout(post, photo), "photo": photo, "placement": placement,
            "backdrop": post.get("backdrop") or "garage", "headline": post.get("overlay_text") or "",
            "subline": post.get("title") or (post.get("product") or {}).get("title", "")[:60],
            "review_text": rv.get("text") if post["kind"] in REVIEW_KINDS else None,
            "rating": rv.get("rating") or 5}


def render_media(post: dict, with_video: bool = True) -> None:
    """Render the graphic for each platform, then the video (pass with_video=False and call queue_video
    to render it in the background instead)."""
    spec = _still_spec(post)
    out_dir = config.MEDIA_DIR / f"post_{post['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = db.utcnow().strftime("%H%M%S")
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    rendered: dict[str, Path] = {}
    for t in post["targets"]:
        fmt = compose.PLATFORM_FORMAT[t["platform"]]
        if fmt not in rendered:
            rendered[fmt] = compose.render_to_file(spec, fmt, out_dir / f"{fmt}_{stamp}.jpg")
        rel = str(rendered[fmt].relative_to(config.DATA_DIR))
        db.run("UPDATE post_targets SET media_path = ?, media_url = NULL WHERE id = ?", (rel, t["id"]))
    if with_video:
        render_video(get_post(post["id"]))


# ================================================================ video (TikTok + Reels)
def video_style(post: dict) -> str:
    if post.get("video_style") in video.STYLES:
        return post["video_style"]
    if post["kind"] in REVIEW_KINDS:
        return "review"
    return "showcase" if post["kind"] in ("showcase", "behind_scenes") else "quick"


def as_reel(post: dict, platform: str, setting_only: bool = False) -> bool:
    """Does this platform get the video as a Reel instead of the image?"""
    if platform not in ("instagram", "facebook"):
        return False
    if post.get("reels") is not None and not setting_only:
        return bool(post["reels"])
    return platform in (db.get_setting("reels_platforms") or [])


def wants_video(post: dict) -> bool:
    return any(t["platform"] == "tiktok" or as_reel(post, t["platform"]) for t in post["targets"])


def main_photo_choice(post: dict) -> str:
    """The post's main photo as a picker value: 'lib:<photo id>' or 'url:<src>'."""
    if post.get("photo_id"):
        return "lib:" + post["photo_id"]
    src = post.get("source_image") or _pick_photo(post)
    return "url:" + src if src else ""


def _library_for_product(product: dict, limit: int = 2) -> list[str]:
    """Your best library photos whose tags name this product's vehicle or design (e.g. 'bronco', 'zebra print').
    Tags that fit much of the catalog ('tow hook covers', 'truck') don't count."""
    def text_of(p):
        return " ".join([p.get("title") or ""] + db.jl(p.get("tags"))).lower()
    text = text_of(product)
    catalog = [text_of(p) for p in db.q("SELECT title, tags FROM products WHERE status = 'active'")]
    cap = max(3, len(catalog) // 20)
    out = []
    for r in db.q("SELECT * FROM photos WHERE hidden = 0 AND score >= 7 AND parent_id IS NULL "
                  "ORDER BY score DESC, taken_at DESC LIMIT 300"):
        n = db.jl(r.get("notes"), {}) or {}
        tags = [t for t in photos.tags_of(r) if len(t) >= 4 and t in text and sum(t in c for c in catalog) <= cap]
        if n.get("shows_product") and tags:
            out.append("lib:" + r["id"])
            if len(out) >= limit:
                break
    return out


def auto_video_choices(post: dict) -> list[str]:
    """Photos for the video when you haven't picked them: the main photo first, then the product's other
    listing photos, the customer's photo, and library photos tagged with this product."""
    out: list[str] = []
    rv = post.get("review") or {}
    if post["kind"] in REVIEW_KINDS and rv.get("image_url"):
        out.append("url:" + rv["image_url"])
    main = main_photo_choice(post)
    if main:
        out.append(main)
    prod = post.get("product")
    if prod:
        out += ["url:" + u for u in db.jl(prod.get("images"))[:4]]
        if post["kind"] not in REVIEW_KINDS:
            out += _library_for_product(prod)
    uniq = list(dict.fromkeys(out))
    return uniq[:4 if post["kind"] in REVIEW_KINDS else 5]


def video_choices(post: dict) -> list[str]:
    return db.jl(post.get("video_photos")) or auto_video_choices(post)


def resolve_choice(value: str) -> str | None:
    if value.startswith("lib:"):
        try:   # the 1600px preview is plenty for a 1080px-wide video; don't wait on iCloud for the original
            return photos.best_image(value[4:], fetch=False)
        except Exception as e:
            db.log(f"Video: library photo {value[4:]} unavailable ({e})", "warn")
            return None
    return value[4:] if value.startswith("url:") and value[4:] else None


def video_file(post_id: int) -> str:
    """The post's newest rendered video, or ''."""
    d = config.MEDIA_DIR / f"post_{post_id}"
    if not d.exists():
        return ""
    vids = sorted((p for p in d.glob("*.mp4") if not p.name.endswith(".part.mp4")), key=lambda p: p.stat().st_mtime)
    return str(vids[-1]) if vids else ""


def render_video(post: dict) -> str:
    """Render the post's video (if any platform wants one) and remove the old one. Returns its path or ''."""
    out_dir = config.MEDIA_DIR / f"post_{post['id']}"
    if not wants_video(post):
        for old in out_dir.glob("*.mp4") if out_dir.exists() else []:
            old.unlink()
        return ""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"video_{db.utcnow().strftime('%H%M%S')}.mp4"
    rv = post.get("review") or {}
    spec = {"photos": [s for s in (resolve_choice(c) for c in video_choices(post)) if s],
            "style": video_style(post), "headline": post.get("overlay_text") or "",
            "subline": post.get("title") or (post.get("product") or {}).get("title", "")[:60],
            "review_text": rv.get("text") if post["kind"] in REVIEW_KINDS else None,
            "rating": rv.get("rating") or 5, "backdrop": post.get("backdrop") or "garage",
            "music": video.pick_track(post.get("video_music") or db.get_setting("video_music"), post["id"]),
            "seed": post["id"]}
    try:
        video.render(spec, out)
    except Exception as e:
        db.log(f"Video for post #{post['id']} failed ({e}); using a slow zoom on the graphic instead", "warn")
        try:
            still = compose.render_to_file(_still_spec(post), "story", out_dir / "video_fallback.jpg")
            video.still_video(still, out)
            still.unlink()
        except Exception as e2:
            db.log(f"Video render failed for post #{post['id']}: {e2}", "error")
            return ""
    for old in out_dir.glob("*.mp4"):
        if old != out and not old.name.endswith(".part.mp4"):
            old.unlink()
    return str(out)


# Videos take 10-60 s, so saving a post renders its video in the background, one at a time per post; saving
# again while one renders queues one more pass with the newest settings.
_video_cv = threading.Condition()
_video_wanted: set[int] = set()
_video_busy: set[int] = set()


def queue_video(post_id: int) -> None:
    with _video_cv:
        _video_wanted.add(post_id)
        if post_id in _video_busy:
            return
        _video_busy.add(post_id)
    threading.Thread(target=_video_worker, args=(post_id,), daemon=True).start()


def _video_worker(post_id: int) -> None:
    while True:
        with _video_cv:
            if post_id not in _video_wanted:
                _video_busy.discard(post_id)
                _video_cv.notify_all()
                return
            _video_wanted.discard(post_id)
        try:
            post = get_post(post_id)
            if post:
                render_video(post)
        except Exception as e:
            db.log(f"Video render failed for post #{post_id}: {e}\n{traceback.format_exc()[-600:]}", "error")


def videos_rendering() -> list[int]:
    with _video_cv:
        return sorted(_video_busy)


def wait_for_video(post_id: int, timeout: float = 600) -> None:
    with _video_cv:
        _video_cv.wait_for(lambda: post_id not in _video_busy, timeout=timeout)


def delete_post(post_id: int) -> None:
    d = config.MEDIA_DIR / f"post_{post_id}"
    if d.exists():
        for f in d.glob("*"):
            f.unlink()
        d.rmdir()
    db.run("DELETE FROM posts WHERE id = ?", (post_id,))


# ================================================================ publishing
def publish_post(post_id: int, only_platform: str | None = None) -> dict:
    s = db.all_settings()
    post = get_post(post_id)
    enabled = set(s.get("enabled_platforms") or [])
    results = {}
    db.run("UPDATE posts SET status = 'publishing', updated_at = ? WHERE id = ?", (db.iso(), post_id))
    wait_for_video(post_id)
    if wants_video(post) and not video_file(post_id):   # e.g. the app restarted mid-render
        render_video(post)
    for t in post["targets"]:
        if only_platform and t["platform"] != only_platform:
            continue
        if t["status"] in ("published", "sent_to_inbox", "skipped"):
            continue
        if t["platform"] not in enabled:
            db.run("UPDATE post_targets SET status='skipped', error='Platform turned off' WHERE id=?", (t["id"],))
            continue
        pub = REGISTRY[t["platform"]]
        local = config.DATA_DIR / (t["media_path"] or "")
        reel = as_reel(post, t["platform"])
        what = " (Reel)" if reel else ""
        try:
            if s.get("dry_run"):
                res_status, ext_id, ext_url = ("sent_to_inbox" if t["platform"] == "tiktok" and s.get("tiktok_mode") == "inbox"
                                               else "published"), "dry-run", ""
            else:
                if not pub.connected():
                    raise PublishError(f"{config.PLATFORM_LABELS[t['platform']]} isn't connected")
                vid = video_file(post_id) if reel or t["platform"] == "tiktok" else ""
                if reel and not vid:
                    raise PublishError("The Reel's video isn't rendered — open the post and save it to re-render")
                media_url = t.get("media_url")
                if pub.needs_public_url and not media_url and not reel:
                    media_url = shopify.upload_image(local, alt=post.get("overlay_text") or "")
                    db.run("UPDATE post_targets SET media_url = ? WHERE id = ?", (media_url, t["id"]))
                res = pub.publish(caption=t["caption"] or "", media_path=str(local), media_url=media_url or "",
                                  title=t.get("title") or "",
                                  link=links.pin_link(post) if t["platform"] == "pinterest" else (t.get("link") or ""),
                                  video_path=vid)
                res_status, ext_id, ext_url = res.status, res.external_id, res.external_url
            db.run("UPDATE post_targets SET status=?, external_id=?, external_url=?, error=NULL, published_at=? "
                   "WHERE id=?", (res_status, ext_id, ext_url, db.iso(), t["id"]))
            results[t["platform"]] = res_status
            db.log(f"{'[dry run] ' if s.get('dry_run') else ''}Post #{post_id} → {t['platform']}{what}: {res_status}")
        except Exception as e:
            db.run("UPDATE post_targets SET status='failed', error=? WHERE id=?", (str(e)[:1000], t["id"]))
            results[t["platform"]] = f"failed: {e}"
            db.log(f"Post #{post_id} → {t['platform']} failed: {e}", "error")
    _rollup_status(post_id)
    if post.get("review_id"):
        db.run("UPDATE reviews SET used_at = ? WHERE id = ?", (db.iso(), post["review_id"]))
    if post.get("photo_id"):
        db.run("UPDATE photos SET used_at = ? WHERE id = ?", (db.iso(), post["photo_id"]))
    if post.get("product_id"):
        db.run("UPDATE products SET last_featured_at = ?, times_featured = COALESCE(times_featured,0) + 1 "
               "WHERE id = ?", (db.iso(), post["product_id"]))
    return results


def _rollup_status(post_id: int) -> None:
    ts = db.q("SELECT status FROM post_targets WHERE post_id = ? AND status != 'skipped'", (post_id,))
    done = [t for t in ts if t["status"] in ("published", "sent_to_inbox")]
    failed = [t for t in ts if t["status"] == "failed"]
    pending = [t for t in ts if t["status"] == "pending"]
    if pending and not done and not failed:
        status = "approved"
    elif done and not failed and not pending:
        status = "published"
    elif done:
        status = "partial"
    else:
        status = "failed"
    db.run("UPDATE posts SET status = ?, updated_at = ? WHERE id = ?", (status, db.iso(), post_id))


def publish_due() -> int:
    if db.get_setting("posting_paused"):
        return 0
    due = db.q("SELECT id FROM posts WHERE status = 'approved' AND scheduled_at IS NOT NULL AND scheduled_at <= ? "
               "ORDER BY scheduled_at", (db.iso(),))
    for p in due:
        try:
            publish_post(p["id"])
        except Exception as e:
            db.log(f"Publishing post #{p['id']} crashed: {e}\n{traceback.format_exc()[-600:]}", "error")
    return len(due)


# ================================================================== metrics
def fetch_metrics() -> int:
    if db.get_setting("dry_run"):
        return 0
    rows = db.q("""SELECT t.*, (SELECT fetched_at FROM metrics m WHERE m.target_id = t.id ORDER BY id DESC LIMIT 1)
                   AS last_fetch, (SELECT COUNT(*) FROM metrics m WHERE m.target_id = t.id) AS n_fetch
                   FROM post_targets t WHERE t.status = 'published' AND t.external_id IS NOT NULL
                   AND t.external_id != 'dry-run' AND t.published_at >= ?""",
                (db.iso(db.utcnow() - timedelta(days=28)),))
    n = 0
    for t in rows:
        age = db.utcnow() - db.parse_iso(t["published_at"])
        last = db.parse_iso(t["last_fetch"])
        # X bills per read: check at ~2 days and ~7 days only. Others: daily for 2 weeks, then weekly.
        if t["platform"] == "x":
            want = (t["n_fetch"] == 0 and age > timedelta(days=2)) or (t["n_fetch"] == 1 and age > timedelta(days=7))
        else:
            gap = timedelta(hours=22) if age < timedelta(days=14) else timedelta(days=7)
            want = last is None or db.utcnow() - last > gap
        if not want:
            continue
        try:
            m = REGISTRY[t["platform"]].metrics(t["external_id"])
        except Exception as e:
            db.log(f"Metrics {t['platform']} #{t['post_id']}: {e}", "warn")
            continue
        if m:
            db.run("""INSERT INTO metrics(target_id, fetched_at, impressions, reach, likes, comments, shares,
                      saves, clicks, views, raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (t["id"], db.iso(), m.impressions, m.reach, m.likes, m.comments, m.shares, m.saves,
                    m.clicks, m.views, json.dumps(m.raw)[:4000]))
            n += 1
    return n


def insights_rows(days: int = 28) -> list[dict]:
    return db.q("""SELECT p.id AS post_id, p.kind, p.backdrop, p.overlay_text AS headline, t.platform,
                   t.published_at, t.external_url, m.impressions, m.reach, m.likes, m.comments, m.shares,
                   m.saves, m.clicks, m.views,
                   COALESCE(m.likes,0)+COALESCE(m.comments,0)+COALESCE(m.shares,0)+COALESCE(m.saves,0) AS engagement
                   FROM post_targets t JOIN posts p ON p.id = t.post_id
                   LEFT JOIN metrics m ON m.id = (SELECT id FROM metrics WHERE target_id = t.id ORDER BY id DESC LIMIT 1)
                   WHERE t.status = 'published' AND t.external_id != 'dry-run' AND t.published_at >= ?
                   ORDER BY t.published_at DESC""", (db.iso(db.utcnow() - timedelta(days=days)),))


def weekly_report() -> dict | None:
    rows = insights_rows()
    if not rows or not brain.available():
        return None
    rep = brain.performance_report(rows)
    db.run("INSERT INTO reports(created_at, kind, body) VALUES(?, 'weekly_performance', ?)",
           (db.iso(), json.dumps(rep)))
    db.set_setting("planner_hint", rep.get("planner_hint", ""))
    db.log("Weekly performance report written")
    return rep
