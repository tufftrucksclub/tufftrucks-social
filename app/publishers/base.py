"""Shared bits for the platform publishers."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx


class PublishError(RuntimeError):
    pass


@dataclass
class Result:
    status: str                      # published | sent_to_inbox
    external_id: str = ""
    external_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Metrics:
    impressions: int | None = None
    reach: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    clicks: int | None = None
    views: int | None = None
    raw: dict = field(default_factory=dict)


class Publisher:
    platform = ""
    needs_public_url = True          # IG/FB/Pinterest fetch the image from a URL

    def configured(self) -> bool:     # app credentials present in .env
        return False

    def connected(self) -> bool:      # account authorized and ready
        return False

    def status_note(self) -> str:     # shown on the Settings page
        return ""

    def publish(self, *, caption: str, media_path: str, media_url: str, title: str = "",
                link: str = "", video_path: str = "") -> Result:
        raise NotImplementedError

    def metrics(self, external_id: str, extra: dict | None = None) -> Metrics | None:
        return None


def request(method: str, url: str, *, retries: int = 3, **kw) -> httpx.Response:
    kw.setdefault("timeout", 60)
    last = None
    for attempt in range(retries):
        try:
            r = httpx.request(method, url, **kw)
        except httpx.HTTPError as e:
            last = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
            continue
        return r
    raise PublishError(f"Network error talking to {url.split('/')[2]}: {last}")


def check(r: httpx.Response, what: str) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:400]}
    if r.status_code >= 400:
        raise PublishError(f"{what} failed ({r.status_code}): {str(body)[:500]}")
    return body
