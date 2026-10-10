"""Facebook Page + Instagram professional account via the Meta Graph API.

Credentials: META_PAGE_ID, META_IG_USER_ID and a token (a System User token from
Business Settings is easiest — it doesn't expire). They can live in .env or be
pasted on the Settings page. Permissions needed: pages_show_list,
pages_read_engagement, pages_manage_posts, instagram_basic,
instagram_content_publish (+ read_insights / instagram_manage_insights for stats).
"""
from __future__ import annotations

import time

from .. import config, db
from .base import Metrics, PublishError, Publisher, Result, check, request


def _creds() -> dict:
    stored = db.get_token("meta") or {}
    return {
        "token": stored.get("access_token") or config.META_ACCESS_TOKEN,
        "page_id": stored.get("page_id") or config.META_PAGE_ID,
        "ig_user_id": stored.get("ig_user_id") or config.META_IG_USER_ID,
    }


def _g(path: str) -> str:
    return f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{path.lstrip('/')}"


def _page_token(c: dict) -> str:
    """Posting to a Page needs a Page token. If we were given a user/system-user token,
    exchange it once for the Page's token and cache it."""
    cached = db.get_token("meta_page") or {}
    if cached.get("page_id") == c["page_id"] and cached.get("access_token"):
        return cached["access_token"]
    r = request("GET", _g(c["page_id"]), params={"fields": "access_token", "access_token": c["token"]})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    tok = body.get("access_token") or c["token"]  # already a page token if no field comes back
    db.save_token("meta_page", {"page_id": c["page_id"], "access_token": tok})
    return tok


class Facebook(Publisher):
    platform = "facebook"

    def configured(self) -> bool:
        c = _creds()
        return bool(c["token"] and c["page_id"])

    def connected(self) -> bool:
        return self.configured()

    def status_note(self) -> str:
        return "" if self.configured() else "Add your Page ID and token on this page or in .env."

    def publish(self, *, caption, media_path, media_url, title="", link="", video_path="") -> Result:
        c = _creds()
        r = request("POST", _g(f"{c['page_id']}/photos"),
                    data={"url": media_url, "caption": caption, "published": "true",
                          "access_token": _page_token(c)})
        body = check(r, "Facebook photo post")
        post_id = body.get("post_id") or body.get("id")
        return Result("published", post_id, f"https://www.facebook.com/{post_id}")

    def metrics(self, external_id, extra=None) -> Metrics | None:
        c = _creds()
        r = request("GET", _g(external_id), params={
            "fields": "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0),shares",
            "access_token": _page_token(c)})
        b = check(r, "Facebook metrics")
        m = Metrics(raw=b)
        m.likes = (b.get("reactions") or {}).get("summary", {}).get("total_count")
        m.comments = (b.get("comments") or {}).get("summary", {}).get("total_count")
        m.shares = (b.get("shares") or {}).get("count", 0)
        return m


class Instagram(Publisher):
    platform = "instagram"

    def configured(self) -> bool:
        c = _creds()
        return bool(c["token"] and c["ig_user_id"])

    def connected(self) -> bool:
        return self.configured()

    def status_note(self) -> str:
        return "" if self.configured() else "Add your Instagram user ID and token (IG must be a Business/Creator account linked to your Page)."

    def publish(self, *, caption, media_path, media_url, title="", link="", video_path="") -> Result:
        c = _creds()
        tok = c["token"]
        r = request("POST", _g(f"{c['ig_user_id']}/media"),
                    data={"image_url": media_url, "caption": caption[:2200], "access_token": tok})
        creation_id = check(r, "Instagram media container")["id"]
        for _ in range(30):
            st = check(request("GET", _g(creation_id), params={"fields": "status_code", "access_token": tok}),
                       "Instagram container status").get("status_code")
            if st == "FINISHED":
                break
            if st in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram couldn't process the image (status {st})")
            time.sleep(3)
        r = request("POST", _g(f"{c['ig_user_id']}/media_publish"),
                    data={"creation_id": creation_id, "access_token": tok})
        media_id = check(r, "Instagram publish")["id"]
        link_r = request("GET", _g(media_id), params={"fields": "permalink", "access_token": tok})
        permalink = link_r.json().get("permalink", "") if link_r.status_code == 200 else ""
        return Result("published", media_id, permalink)

    def metrics(self, external_id, extra=None) -> Metrics | None:
        c = _creds()
        b = check(request("GET", _g(external_id), params={
            "fields": "like_count,comments_count", "access_token": c["token"]}), "Instagram metrics")
        m = Metrics(likes=b.get("like_count"), comments=b.get("comments_count"), raw={"fields": b})
        ins = request("GET", _g(f"{external_id}/insights"),
                      params={"metric": "reach,saved,shares,views", "access_token": c["token"]})
        if ins.status_code == 200:
            vals = {d["name"]: (d.get("values") or [{}])[0].get("value") for d in ins.json().get("data", [])}
            m.reach, m.saves, m.shares, m.views = vals.get("reach"), vals.get("saved"), vals.get("shares"), vals.get("views")
            m.raw["insights"] = vals
        return m
