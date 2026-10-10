"""Pinterest API v5. Free. New apps start on Trial access, where created pins are
only visible to you; request Standard access (with a short screen recording of
connecting + pinning from this app) to make pins public."""
from __future__ import annotations

import base64
from datetime import date, timedelta
from urllib.parse import urlencode

from .. import config, db
from . import oauth
from .base import Metrics, PublishError, Publisher, Result, check, request


def _api() -> str:
    return "https://api-sandbox.pinterest.com/v5" if config.PINTEREST_USE_SANDBOX else "https://api.pinterest.com/v5"


class Pinterest(Publisher):
    platform = "pinterest"

    def configured(self) -> bool:
        return bool(config.PINTEREST_APP_ID and config.PINTEREST_APP_SECRET)

    def connected(self) -> bool:
        return bool(db.get_token("pinterest"))

    def status_note(self) -> str:
        if self.connected() and not db.get_setting("pinterest_board_id"):
            return "Pick a board below."
        return "Pins stay private until Pinterest grants Standard access to your app."

    def _basic(self) -> str:
        raw = f"{config.PINTEREST_APP_ID}:{config.PINTEREST_APP_SECRET}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def auth_url(self) -> str:
        state, _ = oauth.new_state("pinterest")
        return "https://www.pinterest.com/oauth/?" + urlencode({
            "client_id": config.PINTEREST_APP_ID, "redirect_uri": oauth.redirect_uri("pinterest"),
            "response_type": "code", "state": state,
            "scope": "boards:read,pins:read,pins:write,user_accounts:read"})

    def exchange(self, code: str) -> None:
        r = request("POST", f"{_api()}/oauth/token", headers={"Authorization": self._basic()},
                    data={"grant_type": "authorization_code", "code": code,
                          "redirect_uri": oauth.redirect_uri("pinterest")})
        oauth.store("pinterest", check(r, "Pinterest token"))

    def _token(self) -> str:
        tok = db.get_token("pinterest")
        if not tok:
            raise PublishError("Pinterest isn't connected (Settings → Connect Pinterest)")
        if oauth.expiring(tok, minutes=60 * 24):
            r = request("POST", f"{_api()}/oauth/token", headers={"Authorization": self._basic()},
                        data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            tok = oauth.store("pinterest", check(r, "Pinterest token refresh"))
        return tok["access_token"]

    def boards(self) -> list[dict]:
        r = request("GET", f"{_api()}/boards", params={"page_size": 100},
                    headers={"Authorization": f"Bearer {self._token()}"})
        return [{"id": b["id"], "name": b["name"]} for b in check(r, "Pinterest boards").get("items", [])]

    def publish(self, *, caption, media_path, media_url, title="", link="", video_path="") -> Result:
        board = db.get_setting("pinterest_board_id")
        if not board:
            raise PublishError("Choose a Pinterest board on the Settings page")
        body = {"board_id": board, "title": (title or caption)[:100], "description": caption[:500],
                "alt_text": (title or caption)[:500],
                "media_source": {"source_type": "image_url", "url": media_url}}
        if link:
            body["link"] = link
        r = request("POST", f"{_api()}/pins", json=body, headers={"Authorization": f"Bearer {self._token()}"})
        pin = check(r, "Pinterest pin")
        return Result("published", pin["id"], f"https://www.pinterest.com/pin/{pin['id']}/")

    def metrics(self, external_id, extra=None) -> Metrics | None:
        end = date.today()
        start = end - timedelta(days=89)
        r = request("GET", f"{_api()}/pins/{external_id}/analytics", headers={"Authorization": f"Bearer {self._token()}"},
                    params={"start_date": start.isoformat(), "end_date": end.isoformat(),
                            "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"})
        b = check(r, "Pinterest metrics")
        tot = (b.get("all") or {}).get("lifetime_metrics") or (b.get("all") or {}).get("summary_metrics") or {}
        return Metrics(impressions=tot.get("IMPRESSION"), saves=tot.get("SAVE"),
                       clicks=(tot.get("PIN_CLICK") or 0) + (tot.get("OUTBOUND_CLICK") or 0), raw=tot)
