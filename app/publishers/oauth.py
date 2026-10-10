"""OAuth 2.0 helpers (TikTok, Pinterest, X): state + PKCE, token storage, refresh."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta

from .. import config, db


def redirect_uri(platform: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/oauth/{platform}/callback"


def new_state(platform: str, pkce: bool = False) -> tuple[str, str | None]:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)[:96] if pkce else None
    db.save_token(f"oauth_state:{state}", {"platform": platform, "verifier": verifier,
                                            "created_at": db.iso()})
    return state, verifier


def pop_state(state: str) -> dict | None:
    data = db.get_token(f"oauth_state:{state}")
    db.delete_token(f"oauth_state:{state}")
    return data


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def store(platform: str, body: dict, **extra) -> dict:
    data = dict(extra)
    data["access_token"] = body["access_token"]
    if body.get("refresh_token"):
        data["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        data["expires_at"] = db.iso(db.utcnow() + timedelta(seconds=int(body["expires_in"])))
    if body.get("refresh_token_expires_in"):
        data["refresh_expires_at"] = db.iso(db.utcnow() + timedelta(seconds=int(body["refresh_token_expires_in"])))
    old = db.get_token(platform) or {}
    for k in ("open_id", "username", "user_id"):
        if k in old and k not in data:
            data[k] = old[k]
    if "refresh_token" not in data and old.get("refresh_token"):
        data["refresh_token"] = old["refresh_token"]
    db.save_token(platform, data)
    return data


def expiring(tok: dict, minutes: int = 10) -> bool:
    exp = db.parse_iso(tok.get("expires_at"))
    return bool(exp and exp < db.utcnow() + timedelta(minutes=minutes))
