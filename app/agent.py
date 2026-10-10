"""The social media manager itself: sync → plan → draft → render → approve → publish → measure."""
from __future__ import annotations

import json
import random
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import brain, config, db
from .publishers import REGISTRY, PublishError
from .sources import etsy, shopify
from .studio import backdrops, compose

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
    return out


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
    if brain.available():
        try:
            best = shopify.best_sellers() if shopify.configured() else {}
        except Exception:
            best = {}
        plan = brain.plan_week(week_start, products[:120], reviews, best, perf_summary(), styles)
    else:
        plan = _fallback_plan(products, reviews, styles)
        db.log("No ANTHROPIC_API_KEY — used the simple built-in planner", "warn")

    if plan.get("notes"):
        db.run("INSERT INTO reports(created_at, kind, body) VALUES(?, 'plan_notes', ?)",
               (db.iso(), json.dumps({"week": week_key, "notes": plan["notes"]})))

    enabled = set(db.get_setting("enabled_platforms") or config.PLATFORMS)
    product_ids = {p["id"] for p in products}
    review_ids = {r["id"] for r in reviews}
    created, used_days = 0, set()
    for item in plan.get("posts", []):
        day = item.get("day")
        if day not in DAY_KEYS or day in used_days:
            continue
        kind = item.get("kind") if item.get("kind") in config.POST_KINDS else "product_spotlight"
        pid = item.get("product_id") if item.get("product_id") in product_ids else None
        rid = item.get("review_id") if item.get("review_id") in review_ids else None
        if kind in REVIEW_KINDS and not rid:
            continue
        if kind not in REVIEW_KINDS and not pid:
            continue
        sched = _slot(week_start, day)
        if sched <= local_now() + timedelta(hours=1):
            continue
        platforms = [p for p in (item.get("platforms") or config.PLATFORMS) if p in enabled] or list(enabled)
        post_id = create_post(kind=kind, product_id=pid, review_id=rid, angle=item.get("angle", ""),
                              headline=item.get("headline", ""), backdrop=item.get("backdrop", "garage"),
                              scheduled_at=sched, platforms=platforms, created_by="planner", week=week_key)
        used_days.add(day)
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
                source_image=None) -> int:
    now = db.iso()
    post_id = db.run(
        """INSERT INTO posts(kind, title, angle, product_id, review_id, source_image, backdrop, overlay_text,
                             scheduled_at, status, created_by, week, created_at, updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)""",
        (kind, "", angle, product_id, review_id, source_image, backdrop, headline,
         db.iso(scheduled_at) if scheduled_at else None, created_by, week, now, now))
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
    return post


def _layout(post, photo) -> str:
    if post["kind"] in REVIEW_KINDS:
        return "review_photo" if photo else "review_card"
    return "product"


def _pick_photo(post) -> str | None:
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
            caps = brain.draft_captions(post, post.get("product"), post.get("review"))
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
    auto = (post["kind"] in auto_kinds and post["kind"] not in REVIEW_KINDS and not reason and not photo_flag
            and sched is not None and sched >= db.utcnow() + hold)
    status = "approved" if auto else "needs_approval"
    db.run("UPDATE posts SET status = ?, approval = ?, needs_human_reason = ?, updated_at = ? WHERE id = ?",
           (status, "auto" if auto else None, reason or photo_flag or None, db.iso(), post_id))
    return get_post(post_id)


def render_media(post: dict) -> None:
    photo = _pick_photo(post)
    rv = post.get("review") or {}
    placement = "auto"
    if post["kind"] in REVIEW_KINDS:
        placement = (db.jl(rv.get("photo_notes"), {}) or {}).get("placement") or "framed"
    spec = {"layout": _layout(post, photo), "photo": photo, "placement": placement,
            "backdrop": post.get("backdrop") or "garage", "headline": post.get("overlay_text") or "",
            "subline": post.get("title") or (post.get("product") or {}).get("title", "")[:60],
            "review_text": rv.get("text") if post["kind"] in REVIEW_KINDS else None,
            "rating": rv.get("rating") or 5}
    out_dir = config.MEDIA_DIR / f"post_{post['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = db.utcnow().strftime("%H%M%S")
    for old in out_dir.glob("*"):
        old.unlink()
    rendered: dict[str, Path] = {}
    for t in post["targets"]:
        fmt = compose.PLATFORM_FORMAT[t["platform"]]
        if fmt not in rendered:
            rendered[fmt] = compose.render_to_file(spec, fmt, out_dir / f"{fmt}_{stamp}.jpg")
        rel = str(rendered[fmt].relative_to(config.DATA_DIR))
        db.run("UPDATE post_targets SET media_path = ?, media_url = NULL WHERE id = ?", (rel, t["id"]))
    if "story" in rendered:
        try:
            compose.make_video(rendered["story"], out_dir / f"story_{stamp}.mp4")
        except Exception as e:
            db.log(f"Video render failed for post #{post['id']}: {e}", "warn")


def delete_post(post_id: int) -> None:
    d = config.MEDIA_DIR / f"post_{post_id}"
    if d.exists():
        for f in d.glob("*"):
            f.unlink()
        d.rmdir()
    db.run("DELETE FROM posts WHERE id = ?", (post_id,))


# ================================================================ publishing
def _video_for(target) -> str:
    if not target.get("media_path"):
        return ""
    p = config.DATA_DIR / target["media_path"]
    vids = sorted(p.parent.glob("story_*.mp4"))
    return str(vids[-1]) if vids else ""


def publish_post(post_id: int, only_platform: str | None = None) -> dict:
    s = db.all_settings()
    post = get_post(post_id)
    enabled = set(s.get("enabled_platforms") or [])
    results = {}
    db.run("UPDATE posts SET status = 'publishing', updated_at = ? WHERE id = ?", (db.iso(), post_id))
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
        try:
            if s.get("dry_run"):
                res_status, ext_id, ext_url = ("sent_to_inbox" if t["platform"] == "tiktok" and s.get("tiktok_mode") == "inbox"
                                               else "published"), "dry-run", ""
            else:
                if not pub.connected():
                    raise PublishError(f"{config.PLATFORM_LABELS[t['platform']]} isn't connected")
                media_url = t.get("media_url")
                if pub.needs_public_url and not media_url:
                    media_url = shopify.upload_image(local, alt=post.get("overlay_text") or "")
                    db.run("UPDATE post_targets SET media_url = ? WHERE id = ?", (media_url, t["id"]))
                res = pub.publish(caption=t["caption"] or "", media_path=str(local), media_url=media_url or "",
                                  title=t.get("title") or "", link=t.get("link") or "", video_path=_video_for(t))
                res_status, ext_id, ext_url = res.status, res.external_id, res.external_url
            db.run("UPDATE post_targets SET status=?, external_id=?, external_url=?, error=NULL, published_at=? "
                   "WHERE id=?", (res_status, ext_id, ext_url, db.iso(), t["id"]))
            results[t["platform"]] = res_status
            db.log(f"{'[dry run] ' if s.get('dry_run') else ''}Post #{post_id} → {t['platform']}: {res_status}")
        except Exception as e:
            db.run("UPDATE post_targets SET status='failed', error=? WHERE id=?", (str(e)[:1000], t["id"]))
            results[t["platform"]] = f"failed: {e}"
            db.log(f"Post #{post_id} → {t['platform']} failed: {e}", "error")
    _rollup_status(post_id)
    if post.get("review_id"):
        db.run("UPDATE reviews SET used_at = ? WHERE id = ?", (db.iso(), post["review_id"]))
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
