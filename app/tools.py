"""Command-line helpers.  python -m app.tools <command>

  check              Show what's configured and test each connection
  etsy-shop-id NAME  Look up your Etsy shop ID from the shop name
  meta-ids           List your Facebook Pages and linked Instagram account IDs
  sync               Pull products + reviews now
  plan [--replace]   Plan + build next week's posts now
  publish-due        Publish anything approved and due
  test-claude        Make one tiny Claude call to confirm the API key works
  icloud-login       Log in to iCloud Photos here in the terminal (handy on a Pi with no browser)
  photos             Scan iCloud / folders for new photos and have Claude rate them
"""
from __future__ import annotations

import sys

import httpx

from . import agent, brain, config, db
from .publishers import REGISTRY
from .sources import etsy, shopify


def check() -> None:
    db.conn()
    ok = lambda b: "✓" if b else "·"
    print(f"{ok(brain.available())} Claude API key  (model: {config.CLAUDE_MODEL})")
    print(f"{ok(shopify.configured())} Shopify ({config.SHOPIFY_STORE or 'SHOPIFY_STORE not set'})")
    if shopify.configured():
        try:
            d = shopify.gql("{ shop { name } productsCount { count } }")
            print(f"    connected to {d['shop']['name']} — {d['productsCount']['count']} products")
        except Exception as e:
            print(f"    ✗ {e}")
    from .sources import icloud, photos
    ic = icloud.status()
    print(f"{ok(ic['connected'])} iCloud Photos: {ic['apple_id'] or 'not connected'}"
          + (f" — {ic['error']}" if ic["error"] else ""))
    print(f"{ok(bool(photos.folders()))} Photo folders: {', '.join(map(str, photos.folders())) or 'none'}")
    print(f"{ok(etsy.configured())} Etsy (shop {config.ETSY_SHOP_ID or 'ETSY_SHOP_ID not set'})")
    if etsy.configured():
        try:
            d = etsy._get(f"/shops/{config.ETSY_SHOP_ID}")
            print(f"    connected to {d.get('shop_name')} — {d.get('review_count', '?')} reviews")
        except Exception as e:
            print(f"    ✗ {e}")
    for k, p in REGISTRY.items():
        print(f"{ok(p.connected())} {config.PLATFORM_LABELS[k]}: {'connected' if p.connected() else ('app keys set, not connected' if p.configured() else 'not set up')}")
    s = db.all_settings()
    print(f"\nDry run: {s['dry_run']} · Paused: {s['posting_paused']} · Auto-approve: {s['auto_approve_kinds']}")


def etsy_shop_id(name: str) -> None:
    r = httpx.get("https://api.etsy.com/v3/application/shops", params={"shop_name": name},
                  headers=etsy._headers(), timeout=30)
    r.raise_for_status()
    for s in r.json().get("results", []):
        print(f"{s['shop_id']}\t{s['shop_name']}\t{s.get('url', '')}")


def meta_ids() -> None:
    from .publishers.meta import _creds, _g
    c = _creds()
    if not c["token"]:
        sys.exit("Set META_ACCESS_TOKEN in .env (or paste it on the Settings page) first.")
    r = httpx.get(_g("me/accounts"), params={"access_token": c["token"],
                                             "fields": "id,name,instagram_business_account{id,username}"}, timeout=30)
    body = r.json()
    if "error" in body:
        sys.exit(f"Meta said: {body['error'].get('message')}")
    for p in body.get("data", []):
        ig = p.get("instagram_business_account") or {}
        print(f"Page: {p['name']}  META_PAGE_ID={p['id']}")
        print(f"   Instagram: @{ig.get('username', '—')}  META_IG_USER_ID={ig.get('id', '— not linked')}")


def test_claude() -> None:
    import anthropic
    c = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    m = c.messages.create(model=config.CLAUDE_MODEL, max_tokens=30,
                          messages=[{"role": "user", "content": "Say 'ready to post' and nothing else."}])
    print(m.content[0].text)


def icloud_login() -> None:
    import getpass
    from .sources import icloud
    aid = input(f"Apple ID [{icloud.apple_id()}]: ").strip() or icloud.apple_id()
    svc = icloud._service(aid, getpass.getpass("Apple ID password (not saved): "))
    if svc.requires_2fa:
        if getattr(svc, "security_key_names", None):
            print("Touch your security key…")
            svc.confirm_security_key(svc.fido2_devices[0])
        else:
            svc.request_2fa_code()
            if not svc.validate_2fa_code(input("6-digit code from your Apple device: ").strip()):
                sys.exit("That code didn't work.")
        if not svc.is_trusted_session:
            svc.trust_session()
    elif svc.requires_2sa:
        sys.exit("This Apple ID uses old two-step verification; turn on two-factor authentication for it.")
    db.set_setting("icloud_apple_id", aid)
    print(f"Connected — {len(svc.photos.all)} photos in the library. The web app will use this login.")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    db.conn()
    if cmd == "check":
        check()
    elif cmd == "etsy-shop-id":
        etsy_shop_id(" ".join(rest))
    elif cmd == "meta-ids":
        meta_ids()
    elif cmd == "sync":
        print(agent.sync_sources())
    elif cmd == "plan":
        print(agent.plan_week(replace="--replace" in rest))
    elif cmd == "publish-due":
        print(agent.publish_due(), "post(s) processed")
    elif cmd == "test-claude":
        test_claude()
    elif cmd == "icloud-login":
        icloud_login()
    elif cmd == "photos":
        print(agent.sync_photos())
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
