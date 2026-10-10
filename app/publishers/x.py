"""X (Twitter) API v2 with OAuth 2.0 (PKCE). Pay-per-use: roughly 1.5¢ per post,
about 20¢ for a post containing a link, and ~0.5¢ per metrics read — which is why
links are off by default and stats are only pulled a few times per post."""
from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlencode

from .. import config, db
from . import oauth
from .base import Metrics, PublishError, Publisher, Result, check, request

API = "https://api.x.com/2"


class X(Publisher):
    platform = "x"
    needs_public_url = False

    def configured(self) -> bool:
        return bool(config.X_CLIENT_ID)

    def connected(self) -> bool:
        return bool(db.get_token("x"))

    def status_note(self) -> str:
        return "Pay-per-use: ~$0.015/post, ~$0.20 if the post has a link."

    def _auth_header(self) -> dict:
        if config.X_CLIENT_SECRET:
            raw = f"{config.X_CLIENT_ID}:{config.X_CLIENT_SECRET}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        return {}

    def auth_url(self) -> str:
        state, verifier = oauth.new_state("x", pkce=True)
        return "https://x.com/i/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": config.X_CLIENT_ID,
            "redirect_uri": oauth.redirect_uri("x"), "state": state,
            "scope": "tweet.read tweet.write users.read media.write offline.access",
            "code_challenge": oauth.challenge(verifier), "code_challenge_method": "S256"})

    def exchange(self, code: str, verifier: str) -> None:
        data = {"code": code, "grant_type": "authorization_code", "redirect_uri": oauth.redirect_uri("x"),
                "code_verifier": verifier, "client_id": config.X_CLIENT_ID}
        r = request("POST", f"{API}/oauth2/token", data=data, headers=self._auth_header())
        oauth.store("x", check(r, "X token"))
        me = request("GET", f"{API}/users/me", headers={"Authorization": f"Bearer {self._token()}"})
        if me.status_code == 200:
            tok = db.get_token("x")
            tok["username"] = me.json().get("data", {}).get("username", "")
            db.save_token("x", tok)

    def _token(self) -> str:
        tok = db.get_token("x")
        if not tok:
            raise PublishError("X isn't connected (Settings → Connect X)")
        if oauth.expiring(tok):
            r = request("POST", f"{API}/oauth2/token", headers=self._auth_header(), data={
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                "client_id": config.X_CLIENT_ID})
            tok = oauth.store("x", check(r, "X token refresh"))
        return tok["access_token"]

    def publish(self, *, caption, media_path, media_url, title="", link="", video_path="") -> Result:
        if not db.get_setting("x_allow_links") and "http" in caption:
            raise PublishError("X caption contains a link but links are turned off for X (Settings)")
        auth = {"Authorization": f"Bearer {self._token()}"}
        with open(media_path, "rb") as fh:
            up = request("POST", f"{API}/media/upload", headers=auth, timeout=120,
                         files={"media": (Path(media_path).name, fh, "image/jpeg")},
                         data={"media_category": "tweet_image"})
        media_id = check(up, "X media upload")["data"]["id"]
        r = request("POST", f"{API}/tweets", headers=auth,
                    json={"text": caption[:280], "media": {"media_ids": [media_id]}})
        tid = check(r, "X post")["data"]["id"]
        user = (db.get_token("x") or {}).get("username") or "i"
        return Result("published", tid, f"https://x.com/{user}/status/{tid}")

    def metrics(self, external_id, extra=None) -> Metrics | None:
        r = request("GET", f"{API}/tweets/{external_id}", params={"tweet.fields": "public_metrics"},
                    headers={"Authorization": f"Bearer {self._token()}"})
        pm = check(r, "X metrics").get("data", {}).get("public_metrics", {})
        return Metrics(impressions=pm.get("impression_count"), likes=pm.get("like_count"),
                       comments=pm.get("reply_count"),
                       shares=(pm.get("retweet_count") or 0) + (pm.get("quote_count") or 0),
                       saves=pm.get("bookmark_count"), raw=pm)
