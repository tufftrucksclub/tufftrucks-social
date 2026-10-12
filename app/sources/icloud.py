"""iCloud Photos login, via pyicloud (the same private web API icloud.com uses).

Logging in takes your Apple ID password plus a 6-digit code from one of your Apple devices. After that
the session is saved under data/icloud/ and reused, so you only log in again when Apple expires it
(about every two months). The password itself is never saved unless you put ICLOUD_PASSWORD in .env.

Advanced Data Protection blocks this unless "Access iCloud Data on the Web" is turned on
(iPhone → Settings → your name → iCloud).
"""
from __future__ import annotations

import shutil
import threading
import time

from .. import config, db

SESSION_DIR = config.DATA_DIR / "icloud"

_lock = threading.RLock()
_api = None              # logged-in PyiCloudService
_pending = None          # PyiCloudService waiting for its 2FA code
_last_fail = (0.0, "")   # (when, why) of the last failed silent login, so pages don't retry on every load


class NeedsLogin(RuntimeError):
    pass


TERMS_MSG = ("Apple has updated iCloud terms waiting for you to accept. Sign in at icloud.com in your browser "
             "(or check Settings on your iPhone), accept them, then click Connect iCloud here again.")


def installed() -> bool:
    try:
        import pyicloud  # noqa: F401
        return True
    except ImportError:
        return False


def apple_id() -> str:
    return (db.get_setting("icloud_apple_id") or config.ICLOUD_APPLE_ID or "").strip()


def configured() -> bool:
    return installed() and bool(apple_id())


def _service(aid: str, password: str | None):
    from pyicloud import PyiCloudService
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return PyiCloudService(aid, password, cookie_directory=str(SESSION_DIR),
                           china_mainland=True if config.ICLOUD_CHINA else None)


def api():
    """The logged-in session, reusing the saved one. Raises NeedsLogin if you have to log in again."""
    global _api, _last_fail
    with _lock:
        if _api is not None:
            return _api
        if not installed():
            raise NeedsLogin("pyicloud isn't installed (pip install -r requirements.txt)")
        aid = apple_id()
        if not aid:
            raise NeedsLogin("Not connected yet")
        if time.time() - _last_fail[0] < 300:
            raise NeedsLogin(_last_fail[1])
        try:
            svc = _service(aid, config.ICLOUD_PASSWORD or None)
            if svc.requires_2fa or svc.requires_2sa:
                raise NeedsLogin("Apple wants a fresh verification code — log in again")
        except NeedsLogin as e:
            _last_fail = (time.time(), str(e))
            raise
        except Exception as e:
            why = TERMS_MSG if "AcceptTerms" in type(e).__name__ else f"Session expired — log in again ({type(e).__name__})"
            _last_fail = (time.time(), why)
            raise NeedsLogin(why) from e
        _api = svc
        return svc


def drop_session() -> None:
    """Forget the in-memory session (e.g. after Apple rejects a call) so the next api() re-checks."""
    global _api
    with _lock:
        _api = None


PENDING_MINUTES = 10   # Apple's codes expire; after this the login starts over


def start_login(aid: str, password: str) -> str:
    """Returns 'ok' when logged in, or 'code' when Apple sent a code to your devices."""
    global _api, _pending, _last_fail
    aid = aid.strip()
    with _lock:
        _pending = None
        try:
            svc = _service(aid, password)
        except Exception as e:
            db.log(f"iCloud login failed: {type(e).__name__}: {e}", "warn")
            raise RuntimeError(_friendly(e)) from e
        db.set_setting("icloud_apple_id", aid)
        _last_fail = (0.0, "")
        if svc.requires_2fa:
            if getattr(svc, "security_key_names", None):
                raise RuntimeError("This Apple ID uses hardware security keys, which this page can't do. "
                                   "Run  python -m app.tools icloud-login  on this computer instead.")
            try:
                svc.request_2fa_code()
            except Exception as e:  # the code often arrives anyway
                db.log(f"iCloud: asking for a code said {type(e).__name__}: {e}", "warn")
            # The password stays in memory only, so "send a new code" can redo the login. Never written to disk.
            _pending = {"svc": svc, "aid": aid, "password": password, "at": time.time()}
            db.log(f"iCloud: waiting for the verification code ({where_code_went()})")
            return "code"
        if svc.requires_2sa:
            raise RuntimeError("This Apple ID uses the old two-step verification. Turn on two-factor "
                               "authentication for it, or run  python -m app.tools icloud-login.")
        _api = svc
        db.log(f"Connected iCloud Photos ({aid})")
        return "ok"


def _friendly(e: Exception) -> str:
    name, msg = type(e).__name__, str(e)
    if "AcceptTerms" in name:
        return TERMS_MSG
    if "FailedLogin" in name or "invalid" in msg.lower():
        return "Apple didn't accept that Apple ID / password."
    return f"{name}: {msg}" if msg else name


def where_code_went() -> str:
    svc = (_pending or {}).get("svc")
    if svc is None:
        return ""
    try:
        method = svc.two_factor_delivery_method
        note = svc.two_factor_delivery_notice or ""
    except Exception:
        return "your Apple devices"
    where = {"sms": "a text message to your trusted phone number",
             "trusted_device": "a pop-up on your iPhone / iPad / Mac — tap Allow to see the code"}.get(
        method, "your Apple devices (or a text to your phone)")
    return (where + (f". {note}" if note else "")).strip()


def resend_code() -> str:
    """Start the login again with the same password, which makes Apple send a fresh code."""
    p = _pending
    if not p:
        raise RuntimeError("That login expired — enter your password again.")
    return start_login(p["aid"], p["password"])


def cancel_login() -> None:
    global _pending, _last_fail
    with _lock:
        _pending, _last_fail = None, (0.0, "")


def finish_login(code: str) -> bool:
    """True when connected. On a wrong code the login has to start over (Apple's challenge is used up)."""
    global _api, _pending
    with _lock:
        p = _pending
        if p is None:
            raise RuntimeError("That login expired — enter your password again.")
        svc = p["svc"]
        try:
            ok = svc.validate_2fa_code(code.strip().replace(" ", "").replace("-", ""))
        except Exception as e:
            db.log(f"iCloud: checking the code failed: {type(e).__name__}: {e}", "warn")
            if "AcceptTerms" in type(e).__name__:  # the code was fine; Apple wants its new terms accepted
                _pending = None
                raise RuntimeError(TERMS_MSG) from e
            ok = False
        if not ok:
            # pyicloud discards the challenge after one try, so keep the password and get a fresh code ready
            db.log("iCloud: verification code not accepted; requesting a new one", "warn")
            try:
                resend_code()
            except Exception:
                _pending = None
            return False
        if not svc.is_trusted_session:
            try:
                svc.trust_session()
            except Exception as e:
                db.log(f"iCloud: couldn't mark this session trusted ({e}); Apple may ask for a code sooner", "warn")
        _api, _pending = svc, None
        db.log(f"Connected iCloud Photos ({apple_id()})")
        return True


def waiting_for_code() -> bool:
    global _pending
    if _pending and time.time() - _pending["at"] > PENDING_MINUTES * 60:
        _pending = None
    return _pending is not None


def logout() -> None:
    global _api, _pending, _last_fail
    with _lock:
        _api, _pending, _last_fail = None, None, (0.0, "")
        shutil.rmtree(SESSION_DIR, ignore_errors=True)
        db.set_setting("icloud_apple_id", "")


def status(check: bool = True) -> dict:
    """For the Photos page. check=False never touches the network."""
    out = {"installed": installed(), "apple_id": apple_id(), "connected": _api is not None,
           "waiting_for_code": waiting_for_code(), "code_sent_to": where_code_went(), "error": ""}
    if check and not out["connected"] and out["apple_id"] and out["installed"] and not out["waiting_for_code"]:
        try:
            api()
            out["connected"] = True
        except NeedsLogin as e:
            out["error"] = str(e)
    return out
