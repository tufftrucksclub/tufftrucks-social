"""TikTok Content Posting API.

Default mode "inbox": the post's video lands in your TikTok inbox as a draft; you
open TikTok, add a sound, paste the caption (shown in the app) and tap Post.
This works before TikTok audits your developer app.

Mode "direct": posts straight to your profile. Until TikTok audits your app,
TikTok forces these posts to private (only you can see them), so only switch to
direct after the audit passes.
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlencode

from .. import config, db
from . import oauth
from .base import Metrics, PublishError, Publisher, Result, check, request

API = "https://open.tiktokapis.com"


class TikTok(Publisher):
    platform = "tiktok"
    needs_public_url = False

    # ------------------------------------------------------------- auth
    def configured(self) -> bool:
        return bool(config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET)

    def connected(self) -> bool:
        return bool(db.get_token("tiktok"))

    def status_note(self) -> str:
        mode = db.get_setting("tiktok_mode")
        if mode == "direct":
            return "Direct posting: only public once TikTok has audited your developer app."
        return "Inbox mode: drafts arrive in your TikTok inbox; you add a sound and tap Post."

    def auth_url(self) -> str:
        state, _ = oauth.new_state("tiktok")
        scopes = ["user.info.basic", "video.upload", "video.list"]
        if db.get_setting("tiktok_mode") == "direct":
            scopes.append("video.publish")
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
            "client_key": config.TIKTOK_CLIENT_KEY, "response_type": "code",
            "scope": ",".join(scopes), "redirect_uri": oauth.redirect_uri("tiktok"), "state": state})

    def exchange(self, code: str) -> None:
        r = request("POST", f"{API}/v2/oauth/token/", data={
            "client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET,
            "code": code, "grant_type": "authorization_code", "redirect_uri": oauth.redirect_uri("tiktok")},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        body = check(r, "TikTok token")
        if "access_token" not in body:
            raise PublishError(f"TikTok token error: {body}")
        oauth.store("tiktok", body, open_id=body.get("open_id"), scope=body.get("scope"))

    def _token(self) -> str:
        tok = db.get_token("tiktok")
        if not tok:
            raise PublishError("TikTok isn't connected (Settings → Connect TikTok)")
        if oauth.expiring(tok):
            r = request("POST", f"{API}/v2/oauth/token/", data={
                "client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET,
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            body = check(r, "TikTok token refresh")
            if "access_token" not in body:
                raise PublishError(f"TikTok refresh failed — reconnect TikTok. {body}")
            tok = oauth.store("tiktok", body)
        return tok["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json; charset=UTF-8"}

    # ---------------------------------------------------------- publish
    def publish(self, *, caption, media_path, media_url, title="", link="", video_path="") -> Result:
        if not video_path or not Path(video_path).exists():
            raise PublishError("TikTok needs the rendered video (re-render the post)")
        size = Path(video_path).stat().st_size
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
        direct = db.get_setting("tiktok_mode") == "direct"
        if direct:
            info = check(request("POST", f"{API}/v2/post/publish/creator_info/query/", headers=self._headers()),
                         "TikTok creator info").get("data", {})
            opts = info.get("privacy_level_options") or ["SELF_ONLY"]
            privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in opts else opts[0]
            body = {"post_info": {"title": caption[:2200], "privacy_level": privacy,
                                  "disable_comment": False, "disable_duet": False, "disable_stitch": False},
                    "source_info": source}
            init = check(request("POST", f"{API}/v2/post/publish/video/init/", headers=self._headers(), json=body),
                         "TikTok direct post init")
        else:
            init = check(request("POST", f"{API}/v2/post/publish/inbox/video/init/", headers=self._headers(),
                                 json={"source_info": source}), "TikTok inbox upload init")
        err = init.get("error", {})
        if err.get("code") not in (None, "ok"):
            raise PublishError(f"TikTok: {err.get('code')} — {err.get('message')}")
        data = init["data"]
        with open(video_path, "rb") as fh:
            put = request("PUT", data["upload_url"], content=fh.read(), timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes 0-{size - 1}/{size}",
                "Content-Length": str(size)})
        if put.status_code not in (200, 201, 206):
            raise PublishError(f"TikTok upload failed ({put.status_code}): {put.text[:300]}")
        publish_id = data["publish_id"]
        status, post_id = "", ""
        for _ in range(40):
            st = check(request("POST", f"{API}/v2/post/publish/status/fetch/", headers=self._headers(),
                               json={"publish_id": publish_id}), "TikTok status").get("data", {})
            status = st.get("status", "")
            if status == "FAILED":
                raise PublishError(f"TikTok processing failed: {st.get('fail_reason')}")
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                post_id = str(ids[0]) if ids else ""
                break
            time.sleep(4)
        if direct:
            return Result("published", post_id or publish_id, "", {"publish_id": publish_id, "status": status})
        return Result("sent_to_inbox", publish_id, "", {"publish_id": publish_id, "status": status})

    def metrics(self, external_id, extra=None) -> Metrics | None:
        if not external_id or not external_id.isdigit():
            return None  # inbox drafts have no public video id
        r = request("POST", f"{API}/v2/video/query/?fields=id,like_count,comment_count,share_count,view_count",
                    headers=self._headers(), json={"filters": {"video_ids": [external_id]}})
        vids = check(r, "TikTok metrics").get("data", {}).get("videos", [])
        if not vids:
            return None
        v = vids[0]
        return Metrics(likes=v.get("like_count"), comments=v.get("comment_count"),
                       shares=v.get("share_count"), views=v.get("view_count"), raw=v)
