"""Facebook Page + Instagram professional account via the Meta Graph API.

Credentials: META_PAGE_ID, META_IG_USER_ID and a token (a System User token from
Business Settings is easiest — it doesn't expire). They can live in .env or be
pasted on the Settings page. Permissions needed: pages_show_list,
pages_read_engagement, pages_manage_posts, instagram_basic,
instagram_content_publish (+ read_insights / instagram_manage_insights for stats).

Posts go out as a photo, or — when the post is set to go out as a Reel — as the post's rendered video,
uploaded straight from this computer (no public URL needed).
"""
from __future__ import annotations

import time
from pathlib import Path

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


def _rupload(url: str, token: str, path: str, what: str) -> dict:
    """Send a video file to Meta's resumable-upload host in one go."""
    data = Path(path).read_bytes()
    r = request("POST", url, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))},
                content=data, timeout=600)
    return check(r, what)


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
        if video_path:
            return self._reel(caption, video_path)
        c = _creds()
        r = request("POST", _g(f"{c['page_id']}/photos"),
                    data={"url": media_url, "caption": caption, "published": "true",
                          "access_token": _page_token(c)})
        body = check(r, "Facebook photo post")
        post_id = body.get("post_id") or body.get("id")
        return Result("published", post_id, f"https://www.facebook.com/{post_id}")

    def _reel(self, caption: str, video_path: str) -> Result:
        c = _creds()
        tok = _page_token(c)
        start = check(request("POST", _g(f"{c['page_id']}/video_reels"),
                              data={"upload_phase": "start", "access_token": tok}), "Facebook Reel start")
        vid = start["video_id"]
        _rupload(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{config.META_GRAPH_VERSION}/{vid}",
                 tok, video_path, "Facebook Reel upload")
        check(request("POST", _g(f"{c['page_id']}/video_reels"),
                      data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                            "description": caption, "access_token": tok}), "Facebook Reel publish")
        for _ in range(36):   # processing usually takes under a minute; if it's slower, it still goes up
            st = check(request("GET", _g(vid), params={"fields": "status", "access_token": tok}),
                       "Facebook Reel status").get("status") or {}
            phases = [st.get(k) or {} for k in ("uploading_phase", "processing_phase", "publishing_phase")]
            if st.get("video_status") == "error" or any(ph.get("status") == "error" for ph in phases):
                errs = [e.get("message") for ph in phases for e in (ph.get("errors") or [])]
                raise PublishError(f"Facebook couldn't process the Reel: {'; '.join(filter(None, errs)) or st}")
            if (st.get("publishing_phase") or {}).get("status") == "complete":
                break
            time.sleep(5)
        return Result("published", vid, f"https://www.facebook.com/reel/{vid}")

    def metrics(self, external_id, extra=None) -> Metrics | None:
        c = _creds()
        if "_" not in external_id:   # a Reel (video id); photo posts are <page id>_<post id>
            b = check(request("GET", _g(external_id), params={
                "fields": "likes.summary(true).limit(0),comments.summary(true).limit(0)",
                "access_token": _page_token(c)}), "Facebook Reel metrics")
            m = Metrics(raw=b)
            m.likes = (b.get("likes") or {}).get("summary", {}).get("total_count")
            m.comments = (b.get("comments") or {}).get("summary", {}).get("total_count")
            ins = request("GET", _g(f"{external_id}/video_insights"),
                          params={"metric": "blue_reels_play_count", "access_token": _page_token(c)})
            if ins.status_code == 200:
                for d in ins.json().get("data", []):
                    m.views = (d.get("values") or [{}])[0].get("value")
            return m
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
        if video_path:
            r = request("POST", _g(f"{c['ig_user_id']}/media"),
                        data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                              "share_to_feed": "true", "thumb_offset": "1500", "access_token": tok})
            body = check(r, "Instagram Reel container")
            creation_id = body["id"]
            _rupload(body.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{config.META_GRAPH_VERSION}/{creation_id}",
                     tok, video_path, "Instagram Reel upload")
            tries, wait, what = 60, 5, "video"   # Instagram takes a minute or two to process video
        else:
            r = request("POST", _g(f"{c['ig_user_id']}/media"),
                        data={"image_url": media_url, "caption": caption[:2200], "access_token": tok})
            creation_id = check(r, "Instagram media container")["id"]
            tries, wait, what = 30, 3, "image"
        for _ in range(tries):
            st = check(request("GET", _g(creation_id), params={"fields": "status_code,status", "access_token": tok}),
                       "Instagram container status")
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram couldn't process the {what} ({st.get('status') or st.get('status_code')})")
            time.sleep(wait)
        else:
            raise PublishError(f"Instagram was still processing the {what} after {tries * wait // 60} minutes — retry later")
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
