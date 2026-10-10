"""The agent's thinking: plans the week, writes captions per platform, sizes up
review photos, and reviews what performed. All calls go to the Claude API with a
forced tool call so the output is always structured JSON."""
from __future__ import annotations

import base64
import io
import json
from datetime import date, timedelta

from PIL import Image

from . import config, db

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
    client = _client()
    msg = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user_content}],
    )
    for block in msg.content:
        if getattr(block, "type", "") == "tool_use":
            return block.input
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
                        "angle": {"type": "string", "description": "The hook/idea in one sentence."},
                        "headline": {"type": "string", "description": "Text burned onto the graphic, max 6 words, punchy."},
                        "backdrop": {"type": "string"},
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
              perf_summary: str, backdrop_styles: list[str]) -> dict:
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
- behind_scenes: making-of / family-run / made-to-order story. Use a product photo.

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


def draft_captions(post: dict, product: dict | None, review: dict | None) -> dict:
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
    parts.append(x_rule)
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
