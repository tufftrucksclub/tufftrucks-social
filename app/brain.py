"""The agent's thinking: plans the week, writes captions per platform, sizes up
review photos, and reviews what performed. All calls go to the Claude API and ask for
a tool call so the output is always structured JSON."""
from __future__ import annotations

import base64
import io
import json
from datetime import date, timedelta

from PIL import Image

from . import config, db, links

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class BrainError(RuntimeError):
    pass


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def _client():
    if not available():
        raise BrainError("ANTHROPIC_API_KEY is not set in .env")
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _brand_block(s: dict) -> str:
    return (
        f"Business: {s['brand_name']} ({config.STORE_URL}) — custom, made-to-order aftermarket tow hook "
        f"covers and accessories for trucks and SUVs, sold on Shopify and Etsy.\n"
        f"Voice: {s['brand_voice']}\n"
        f"Always-use hashtags: {' '.join(s['brand_hashtags'])}\n"
        f"Rules: {s['avoid']}"
    )


def _call(system: str, user_content, tool: dict, max_tokens: int = 4000) -> dict:
    # Newer models reject a forced tool_choice ("tool"/"any"), so let the model choose
    # ("auto"), tell it plainly to answer with the tool, and nudge once if it doesn't.
    client = _client()
    system = (system + f"\n\nAlways respond by calling the `{tool['name']}` tool exactly once. "
              "Do not answer in plain text.")
    messages = [{"role": "user", "content": user_content}]
    for _ in range(2):
        msg = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "auto"},
            messages=messages,
        )
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use":
                return block.input
        messages = messages + [
            {"role": "assistant", "content": msg.content},
            {"role": "user", "content": f"Please submit your answer by calling the `{tool['name']}` tool."},
        ]
    raise BrainError("Claude did not return structured output")


# ------------------------------------------------------------ weekly plan
PLAN_TOOL = {
    "name": "submit_week_plan",
    "description": "Submit the social media plan for the week.",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {"type": "string", "description": "2-4 sentences on the week's theme and why."},
            "posts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string", "enum": DAYS},
                        "kind": {"type": "string", "enum": list(config.POST_KINDS)},
                        "product_id": {"type": "string", "description": "id from the catalog, or empty"},
                        "review_id": {"type": "string", "description": "id from the reviews list, or empty"},
                        "photo_id": {"type": "string", "description": "id from your photo library, or empty"},
                        "angle": {"type": "string", "description": "The hook/idea in one sentence."},
                        "headline": {"type": "string", "description": "Text burned onto the graphic, max 6 words, punchy."},
                        "backdrop": {"type": "string"},
                        "video_style": {"type": "string", "enum": ["quick", "showcase", "review"],
                                        "description": "How the post's short video (TikTok / Reels) moves."},
                        "video_photo_ids": {"type": "array", "items": {"type": "string"},
                                            "description": "Up to 3 more library photo ids to show in the video "
                                                           "(other angles of the same product or truck), or empty."},
                        "platforms": {"type": "array", "items": {"type": "string", "enum": config.PLATFORMS}},
                        "why": {"type": "string"},
                    },
                    "required": ["day", "kind", "angle", "headline", "backdrop", "platforms"],
                },
            },
        },
        "required": ["notes", "posts"],
    },
}


def plan_week(week_start: date, products: list[dict], reviews: list[dict], best: dict[str, int],
              perf_summary: str, backdrop_styles: list[str], photos: list[dict] | None = None) -> dict:
    s = db.all_settings()
    catalog = [{
        "id": p["id"], "title": p["title"], "type": p.get("product_type") or "", "price": p.get("price"),
        "tags": db.jl(p.get("tags"))[:8], "has_images": bool(db.jl(p.get("images"))),
        "sold_last_30d": best.get(p["id"], 0), "times_featured": p.get("times_featured") or 0,
        "last_featured": (p.get("last_featured_at") or "")[:10] or None,
    } for p in products]
    rv = [{
        "id": r["id"], "rating": r["rating"], "text": (r["text"] or "")[:400],
        "product": r.get("product_title"), "has_photo": bool(r.get("image_url")),
        "photo_score": r.get("photo_score"), "date": (r.get("created_at") or "")[:10],
    } for r in reviews]
    lib = [{
        "id": p["id"], "score": p.get("score"), "what": p["n"].get("description"), "subject": p["n"].get("subject"),
        "shows_product": p["n"].get("shows_product"), "background": p["n"].get("background"),
        "tags": p["n"].get("tags"),
        "taken": (p.get("taken_at") or "")[:10],
    } for p in (photos or [])]
    week_end = week_start + timedelta(days=6)
    user = f"""Plan social posts for the week of {week_start:%A %B %d, %Y} through {week_end:%A %B %d}.

Targets: {s['posts_per_week']} posts this week, of which about {s['review_posts_per_week']} feature customer reviews.
Usual posting days/times (local): {json.dumps(s['posting_times'])}. Use one post per day max.
Enabled platforms: {', '.join(s['enabled_platforms'])}. Most posts should go to all enabled platforms;
drop a platform only when the content truly doesn't fit it.

Post kinds:
- product_spotlight: one product, photo from the listing, new backdrop. (These can auto-publish, so keep them
  factual and safe.)
- review: a customer review as a quote card (with their photo if it has one).
- review_photo: the customer's photo is the star, placed on an exciting backdrop, review quote underneath.
  Prefer reviews with has_photo=true and a high photo_score.
- promo: only if there's an obvious seasonal hook this week (holiday, season change). Never invent discounts.
- behind_scenes: making-of / family-run / made-to-order story. Use a product photo, or a library photo of the
  shop / making things if there is one.
- showcase: a great photo from the owner's own library (a truck wearing our covers, an install, the shop) is the
  star. Needs photo_id. Add product_id too when you can tell which product is in it.
Any non-review post may set photo_id to use a library photo instead of the listing photo. Prefer high scores and
photos that show our products; don't use the same photo twice.

Every post also becomes a 6-20 second vertical video (TikTok, and Reels on Instagram/Facebook) made from its
photos. video_style: quick = fast cuts with a punch-in on each photo (product spotlights, promos; best when there are
several photos); showcase = slow smooth moves and crossfades (one great photo, builds, behind the scenes); review =
the customer's quote types in over their photo (review posts). The video opens with the headline, so make it a
scroll-stopping hook. Add video_photo_ids when the library has more shots of the same thing.

Backdrop styles available: {', '.join(backdrop_styles)}.
Match the backdrop to the vibe (e.g. neon for bold colors, trail for off-road builds, garage for gritty,
studio for clean product shots, racing for sporty). Vary them across the week.

Choose products by mixing: recent best sellers, things not featured lately, new arrivals, and seasonal fit.
Don't feature the same product twice in one week. Shopify products and Etsy listings are often the same item
(near-identical titles) — treat them as one and prefer the Shopify id, since that links to the store.

What performed recently:
{perf_summary or 'No performance data yet.'}

CATALOG ({len(catalog)} products):
{json.dumps(catalog, separators=(',', ':'))}

UNUSED 4-5 STAR REVIEWS ({len(rv)}):
{json.dumps(rv, separators=(',', ':'))}

YOUR PHOTO LIBRARY — best unused shots ({len(lib)}):
{json.dumps(lib, separators=(',', ':')) if lib else 'None yet.'}
"""
    system = ("You are the social media manager for a small family business. You plan a realistic week of "
              "posts that sell product and build community.\n\n" + _brand_block(s))
    return _call(system, user, PLAN_TOOL, max_tokens=6000)


# ------------------------------------------------------------ captions
CAPTION_TOOL = {
    "name": "submit_captions",
    "description": "Submit platform-specific copy for one post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instagram": {"type": "string", "description": "Hook first line; 1-3 short paragraphs; 'link in bio'; 5-10 hashtags at the end."},
            "facebook": {"type": "string", "description": "Conversational, 1-3 short paragraphs, ends with the product link. 0-3 hashtags."},
            "tiktok": {"type": "string", "description": "Under 150 characters plus 3-5 hashtags."},
            "pinterest_title": {"type": "string", "description": "Max 90 characters, searchable (vehicle + product)."},
            "pinterest": {"type": "string", "description": "Max 450 characters, keyword-rich description."},
            "x": {"type": "string", "description": "Max 260 characters including 1-2 hashtags."},
            "headline": {"type": "string", "description": "Graphic headline, max 6 words."},
            "subline": {"type": "string", "description": "Short line for the graphic, e.g. product + vehicle fitment, max 8 words."},
            "needs_human": {"type": "boolean", "description": "True if anything is uncertain: fitment unclear, claims you can't verify, sensitive content, or the source text is odd."},
            "needs_human_reason": {"type": "string"},
        },
        "required": ["instagram", "facebook", "tiktok", "pinterest_title", "pinterest", "x",
                     "headline", "subline", "needs_human"],
    },
}


def draft_captions(post: dict, product: dict | None, review: dict | None, photo: dict | None = None) -> dict:
    s = db.all_settings()
    link = (product or {}).get("url") or config.STORE_URL
    x_rule = (f"Include this link in the X post: {link}" if s.get("x_allow_links")
              else "Do NOT put any URL in the X post (links cost extra there). Say 'link in bio' or nothing.")
    parts = [f"Post kind: {config.POST_KINDS.get(post['kind'], post['kind'])}",
             f"Angle: {post.get('angle') or ''}",
             f"Planned headline: {post.get('overlay_text') or ''}",
             f"Product link: {link}"]
    if product:
        parts.append("PRODUCT:\n" + json.dumps({
            "title": product["title"], "price": product.get("price"), "type": product.get("product_type"),
            "tags": db.jl(product.get("tags"))[:10], "description": (product.get("description") or "")[:900],
            "sold_on": product.get("source")}, indent=1))
    if review:
        parts.append("CUSTOMER REVIEW (quote it faithfully, never name the buyer — Etsy doesn't share names):\n"
                     + json.dumps({"rating": review["rating"], "text": review["text"],
                                   "product": review.get("product_title")}, indent=1))
    if photo:
        parts.append("THE PHOTO (from the owner's own library — describe only what's actually in it):\n"
                     + json.dumps({k: photo.get(k) for k in ("description", "subject", "shows_product", "edit")
                                   if photo.get(k) is not None}, indent=1))
    parts.append(x_rule)
    pin_mode = links.pin_mode()
    if pin_mode == "etsy":
        parts.append("The pin links to our Etsy listing: in the Pinterest title and description don't mention a website "
                     "or URL; 'tap through to our Etsy shop' or 'find it on Etsy' is fine.")
    elif pin_mode == "none":
        parts.append("Pinterest pins have no link for now: don't mention the website, a URL or 'link' in the Pinterest "
                     "title or description — say 'search Tuff Trucks Club' or nothing.")
    parts.append("Don't invent fitment years, discounts, prices or shipping times that aren't in the data above.")
    system = ("You write social posts for a small truck-accessory shop. Each platform gets copy written for "
              "how people use that platform, not the same text pasted five times.\n\n" + _brand_block(s))
    out = _call(system, "\n\n".join(parts), CAPTION_TOOL, max_tokens=3000)
    if not s.get("x_allow_links") and "http" in out.get("x", ""):
        out["needs_human"] = True
        out["needs_human_reason"] = ((out.get("needs_human_reason") or "") + " X copy contains a link.").strip()
    return out


# ------------------------------------------------------------ review photos
PHOTO_TOOL = {
    "name": "rate_photo",
    "description": "Rate a customer photo for use in a social post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": "How well this works as a social post: sharpness, lighting, product visible, wow factor."},
            "shows_product": {"type": "boolean"},
            "privacy_issue": {"type": "boolean", "description": "A readable license plate, a face, a house number or other personal info is visible."},
            "suggested_backdrop": {"type": "string"},
            "placement": {"type": "string", "enum": ["framed", "cutout"],
                          "description": "cutout if the product/vehicle is a clean subject that would look good with the background removed; otherwise framed."},
            "notes": {"type": "string", "description": "One sentence."},
        },
        "required": ["score", "shows_product", "privacy_issue", "placement", "notes"],
    },
}


def _img_block(img: Image.Image) -> dict:
    img = img.convert("RGB")
    img.thumbnail((1024, 1024), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def rate_review_photo(img: Image.Image, review_text: str, styles: list[str]) -> dict:
    content = [_img_block(img), {"type": "text", "text":
               f"Customer review text: {review_text[:400]}\nBackdrop styles: {', '.join(styles)}\n"
               "Rate this photo for a truck-accessory shop's social post."}]
    return _call("You are a careful social media photo editor.", content, PHOTO_TOOL, max_tokens=600)


# ------------------------------------------------------------ photo library
LIBRARY_TOOL = {
    "name": "rate_photos",
    "description": "Rate each photo from the owner's own photo library for use in a social post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "photos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The photo's number as given."},
                        "score": {"type": "integer", "minimum": 1, "maximum": 10,
                                  "description": "How well it works as a post for this shop: on-topic (trucks, our "
                                                 "products, installs, the shop), sharp, well lit, a clear subject. "
                                                 "Off-topic photos (family, food, screenshots, receipts) get 1-2."},
                        "description": {"type": "string", "description": "What's in it, one short sentence."},
                        "subject": {"type": "string", "enum": ["truck_with_product", "product_closeup", "truck",
                                                               "making_or_shop", "people", "other"]},
                        "shows_product": {"type": "boolean", "description": "Our tow hook covers or another product of ours is visible."},
                        "privacy_issue": {"type": "boolean", "description": "Readable license plate, a recognizable face, a house number, a screen or other personal info."},
                        "background": {"type": "string", "enum": ["great", "ok", "mundane"],
                                       "description": "mundane = parking lot, garage clutter, driveway, store lot: a new background would help."},
                        "cutout_ok": {"type": "boolean", "description": "The main subject is whole and clearly separated, so it could be cut out and put on a new background."},
                        "suggested_backdrop": {"type": "string", "description": "One of the backdrop styles given."},
                        "scene_idea": {"type": "string", "description": "A short, photorealistic background scene that would make this pop, e.g. 'rocky mountain overlook at golden hour'."},
                        "headline_idea": {"type": "string", "description": "Graphic headline, max 6 words."},
                        "tags": {"type": "array", "items": {"type": "string"},
                                 "description": "3-6 short lowercase tags the owner would filter by: vehicle make/model "
                                                "(e.g. 'bronco', 'f-150'), vehicle color, the cover's design or pattern "
                                                "(e.g. 'zebra print', 'thin blue line'), setting ('night', 'snow', 'trail')."},
                    },
                    "required": ["index", "score", "description", "subject", "shows_product", "privacy_issue",
                                 "background", "cutout_ok"],
                },
            },
        },
        "required": ["photos"],
    },
}


def rate_library_photos(images: list[Image.Image], styles: list[str]) -> list[dict]:
    """Rate a batch of photos in one call (cheaper than one call each). Returns one dict per photo, in order."""
    s = db.all_settings()
    content: list[dict] = []
    for i, img in enumerate(images, 1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        small = img.convert("RGB")
        small.thumbnail((768, 768), Image.LANCZOS)
        content.append(_img_block(small))
    content.append({"type": "text", "text":
                    f"These {len(images)} photos are from the shop owner's phone. Rate every one (index 1-{len(images)}).\n"
                    f"Backdrop styles: {', '.join(styles)}"})
    system = ("You are a sharp social media photo editor for a small truck-accessory shop. You pick the photos "
              "that would stop a truck guy's thumb, and you're honest about the ones that wouldn't.\n\n"
              + _brand_block(s))
    out = _call(system, content, LIBRARY_TOOL, max_tokens=400 + 250 * len(images))
    by_index = {int(p.get("index", 0)): p for p in out.get("photos", []) if isinstance(p, dict)}
    return [by_index.get(i, {}) for i in range(1, len(images) + 1)]


# ------------------------------------------------------------ performance
REPORT_TOOL = {
    "name": "submit_report",
    "description": "Weekly performance review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Plain-English review in markdown: what worked, what didn't, by platform and post kind. Under 250 words."},
            "do_more": {"type": "array", "items": {"type": "string"}},
            "do_less": {"type": "array", "items": {"type": "string"}},
            "planner_hint": {"type": "string", "description": "One paragraph the planner will read next week."},
        },
        "required": ["summary", "do_more", "do_less", "planner_hint"],
    },
}


def performance_report(rows: list[dict]) -> dict:
    s = db.all_settings()
    user = ("Here are the posts from the last 28 days with their latest metrics (engagement = likes + comments "
            "+ shares + saves). Some platforms report fewer fields. Be honest when the sample is too small to "
            "conclude anything.\n\n" + json.dumps(rows, separators=(',', ':')))
    return _call("You analyze social media results for a small business owner who wants straight talk.\n\n"
                 + _brand_block(s), user, REPORT_TOOL, max_tokens=2500)
