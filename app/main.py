"""Web app: the control panel for the social media manager."""
from __future__ import annotations

import json
import re
import threading
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import agent, brain, config, db, scheduler
from .publishers import REGISTRY
from .publishers import oauth as oauth_mod
from .sources import etsy, icloud, shopify
from .sources import photos as photos_mod
from .studio import backdrops, bgswap, compose

BASE = Path(__file__).parent
app = FastAPI(title="Tuff Trucks Social")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/fonts", StaticFiles(directory=BASE / "studio" / "fonts"), name="fonts")
templates = Jinja2Templates(directory=BASE / "templates")

# ------------------------------------------------------------ background jobs
_jobs: dict[str, dict] = {}


def run_bg(name: str, label: str, fn, *args, **kw) -> None:
    if _jobs.get(name, {}).get("running"):
        return

    def work():
        _jobs[name] = {"running": True, "label": label, "started": db.iso()}
        try:
            res = fn(*args, **kw)
            _jobs[name] = {"running": False, "label": label, "result": res, "finished": db.iso()}
        except Exception as e:
            db.log(f"{label} failed: {e}\n{traceback.format_exc()[-800:]}", "error")
            _jobs[name] = {"running": False, "label": label, "error": str(e), "finished": db.iso()}

    threading.Thread(target=work, daemon=True).start()


def running_jobs() -> list[str]:
    return [j["label"] for j in _jobs.values() if j.get("running")]


# ------------------------------------------------------------------ helpers
_NO_PAD = re.compile(r"%-([a-zA-Z])")


def strf(dt, fmt: str) -> str:
    """strftime that understands %-d / %-I (no leading zero) everywhere — Windows doesn't."""
    return dt.strftime(_NO_PAD.sub(lambda m: dt.strftime("%" + m.group(1)).lstrip("0") or "0", fmt))


def fmt_local(iso_s: str | None, fmt: str = "%a %b %-d · %-I:%M %p") -> str:
    dt = db.parse_iso(iso_s)
    return strf(dt.astimezone(agent.tz()), fmt) if dt else "—"


def media_url(rel: str | None) -> str:
    if not rel:
        return ""
    return "/media/" + quote(rel)


def img_src(src: str | None) -> str:
    """URL for showing a source photo (remote URL or local file) in the browser."""
    if not src:
        return ""
    if src.startswith("http"):
        return src
    p = Path(src)
    try:
        rel = p.relative_to(config.DATA_DIR)
    except ValueError:
        return ""
    return media_url(str(rel))


def connections() -> list[dict]:
    out = []
    enabled = set(db.get_setting("enabled_platforms") or [])
    for key, pub in REGISTRY.items():
        out.append({"key": key, "label": config.PLATFORM_LABELS[key], "configured": pub.configured(),
                    "connected": pub.connected(), "enabled": key in enabled, "note": pub.status_note(),
                    "oauth": hasattr(pub, "auth_url")})
    return out


def ctx(request: Request, **kw) -> dict:
    s = db.all_settings()
    base = {"request": request, "s": s, "cfg": config, "labels": config.PLATFORM_LABELS,
            "kinds": config.POST_KINDS, "fmt_local": fmt_local, "media_url": media_url, "img_src": img_src,
            "jl": db.jl, "jobs": running_jobs(), "brain_ok": brain.available(),
            "flash": request.query_params.get("msg", ""), "nav": kw.pop("nav", "")}
    base.update(kw)
    return base


def back(url: str, msg: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}msg={quote(msg)}" if msg else ""), status_code=303)


def thumb(post: dict) -> str:
    for t in post.get("targets") or []:
        if t["platform"] == "instagram" and t.get("media_path"):
            return media_url(t["media_path"])
    for t in post.get("targets") or []:
        if t.get("media_path"):
            return media_url(t["media_path"])
    return ""


def posts_where(where: str, params: tuple = (), order: str = "scheduled_at") -> list[dict]:
    rows = db.q(f"SELECT id FROM posts WHERE {where} ORDER BY {order}", params)
    out = []
    for r in rows:
        p = agent.get_post(r["id"])
        p["thumb"] = thumb(p)
        out.append(p)
    return out


templates.env.globals.update(thumb=thumb, strf=strf)


# ----------------------------------------------------------------- startup
@app.on_event("startup")
def startup():
    db.conn()
    if config.DEMO_MODE:
        from . import demo
        demo.seed()
    if config.env("DISABLE_SCHEDULER", "0") != "1":
        scheduler.start()


# ---------------------------------------------------------------- media
@app.get("/media/{rel:path}")
def media(rel: str):
    p = (config.DATA_DIR / rel).resolve()
    if not str(p).startswith(str(config.DATA_DIR.resolve())) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ------------------------------------------------- Store Manager badge
@app.get("/api/todo")
def api_todo():
    """What needs Daryl, for the Social button in Store Manager (same idea as Accounting's /api/todo).
    No login on this app, so plain CORS is enough for Store Manager on :3000 to read it."""
    soon = db.iso(db.utcnow() + timedelta(hours=24))
    waiting = db.q1("SELECT COUNT(*) n FROM posts WHERE status='needs_approval'")["n"]
    due_soon = db.q1("SELECT COUNT(*) n FROM posts WHERE status='needs_approval' AND scheduled_at <= ?", (soon,))["n"]
    problems = db.q1("SELECT COUNT(*) n FROM posts WHERE status IN ('failed','partial')")["n"]
    drafts = db.q1("""SELECT COUNT(*) n FROM post_targets WHERE platform='tiktok' AND status='sent_to_inbox'
                      AND published_at >= ?""", (db.iso(db.utcnow() - timedelta(days=3)),))["n"]
    items = []
    if waiting:
        items.append({"text": f"{waiting} post{'s' if waiting != 1 else ''} waiting for your OK"
                      + (f" ({due_soon} due within a day)" if due_soon else ""), "urgent": bool(due_soon)})
    if problems:
        items.append({"text": f"{problems} post{'s' if problems != 1 else ''} failed to publish", "urgent": True})
    if drafts:
        items.append({"text": f"{drafts} TikTok draft{'s' if drafts != 1 else ''} to finish in the app", "urgent": False})
    return JSONResponse({"count": waiting + problems + drafts, "urgent": any(i["urgent"] for i in items),
                         "items": items},
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    now = db.iso()
    pending = posts_where("status = 'needs_approval'")
    upcoming = posts_where("status IN ('approved','publishing') AND scheduled_at >= ?", (db.iso(db.utcnow() - timedelta(hours=1)),))[:12]
    problems = posts_where("status IN ('failed','partial')", order="updated_at DESC")[:8]
    recent = posts_where("status IN ('published','partial')", order="updated_at DESC")[:8]
    inbox = db.q("""SELECT t.*, p.overlay_text FROM post_targets t JOIN posts p ON p.id = t.post_id
                    WHERE t.platform='tiktok' AND t.status='sent_to_inbox' AND t.published_at >= ?
                    ORDER BY t.published_at DESC LIMIT 6""", (db.iso(db.utcnow() - timedelta(days=3)),))
    activity = db.q("SELECT * FROM activity ORDER BY id DESC LIMIT 25")
    counts = {
        "products": db.q1("SELECT COUNT(*) n FROM products WHERE status='active'")["n"],
        "reviews": db.q1("SELECT COUNT(*) n FROM reviews WHERE hidden=0")["n"],
        "review_photos": db.q1("SELECT COUNT(*) n FROM reviews WHERE hidden=0 AND image_url IS NOT NULL AND image_url != ''")["n"],
        "published_7d": db.q1("SELECT COUNT(*) n FROM post_targets WHERE status IN ('published','sent_to_inbox') AND published_at >= ?",
                              (db.iso(db.utcnow() - timedelta(days=7)),))["n"],
    }
    plan_note = db.q1("SELECT body FROM reports WHERE kind='plan_notes' ORDER BY id DESC LIMIT 1")
    return templates.TemplateResponse(request, "dashboard.html", ctx(
        request, nav="home", pending=pending, upcoming=upcoming, problems=problems, recent=recent, inbox=inbox,
        activity=activity, counts=counts, conns=connections(), next_runs=scheduler.next_runs(),
        plan_note=json.loads(plan_note["body"]) if plan_note else None, now=now))


@app.post("/actions/sync")
def action_sync():
    run_bg("sync", "Syncing Shopify + Etsy", agent.sync_sources)
    return back("/", "Syncing products and reviews in the background…")


@app.post("/actions/plan")
def action_plan(week: str = Form("next"), replace: str = Form("")):
    start = agent.next_week_start()
    if week == "this":
        today = agent.local_now().date()
        start = today - timedelta(days=today.weekday())
    run_bg("plan", "Planning the week", agent.plan_week, start, bool(replace))
    return back("/", "Planning the week — this takes a minute or two. Refresh to see posts appear.")


@app.post("/actions/publish-due")
def action_publish_due():
    run_bg("publish", "Publishing due posts", agent.publish_due)
    return back("/", "Checking for posts that are due…")


@app.post("/actions/metrics")
def action_metrics():
    run_bg("metrics", "Fetching stats", agent.fetch_metrics)
    return back("/insights", "Fetching the latest stats…")


@app.post("/actions/report")
def action_report():
    run_bg("report", "Writing performance report", agent.weekly_report)
    return back("/insights", "Writing the performance review…")


@app.post("/actions/pause")
def action_pause(on: str = Form("1")):
    db.set_setting("posting_paused", on == "1")
    return back("/", "All posting paused." if on == "1" else "Posting resumed.")


@app.get("/api/jobs")
def api_jobs():
    return JSONResponse({"running": running_jobs()})


# ---------------------------------------------------------------- calendar
@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, start: str = ""):
    today = agent.local_now().date()
    d0 = date.fromisoformat(start) if start else today - timedelta(days=today.weekday())
    d0 = d0 - timedelta(days=d0.weekday())
    lo = datetime.combine(d0, datetime.min.time(), tzinfo=agent.tz())
    hi = lo + timedelta(days=7)
    posts = posts_where("scheduled_at >= ? AND scheduled_at < ?", (db.iso(lo), db.iso(hi)))
    days = []
    for i in range(7):
        d = d0 + timedelta(days=i)
        days.append({"date": d, "is_today": d == today,
                     "posts": [p for p in posts if db.parse_iso(p["scheduled_at"]).astimezone(agent.tz()).date() == d]})
    return templates.TemplateResponse(request, "calendar.html", ctx(
        request, nav="calendar", days=days, prev=(d0 - timedelta(days=7)).isoformat(),
        next=(d0 + timedelta(days=7)).isoformat(), week_label=f"{strf(d0, '%b %-d')} – {strf(d0 + timedelta(days=6), '%b %-d, %Y')}"))


# ---------------------------------------------------------------- posts
@app.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int):
    post = agent.get_post(post_id)
    if not post:
        raise HTTPException(404)
    sched = db.parse_iso(post["scheduled_at"])
    local_val = sched.astimezone(agent.tz()).strftime("%Y-%m-%dT%H:%M") if sched else ""
    video = ""
    tt = next((t for t in post["targets"] if t["platform"] == "tiktok"), None)
    if tt:
        v = agent._video_for(tt)
        if v:
            video = media_url(str(Path(v).relative_to(config.DATA_DIR)))
    metrics = {t["id"]: db.q1("SELECT * FROM metrics WHERE target_id=? ORDER BY id DESC LIMIT 1", (t["id"],))
               for t in post["targets"]}
    return templates.TemplateResponse(request, "post.html", ctx(
        request, nav="calendar", post=post, local_val=local_val, styles=backdrops.list_styles(),
        video=video, metrics=metrics, photo=agent._pick_photo(post), placements=PLACEMENTS))


@app.post("/posts/{post_id}/save")
async def post_save(request: Request, post_id: int):
    form = await request.form()
    post = agent.get_post(post_id)
    if not post:
        raise HTTPException(404)
    sched = form.get("scheduled_at")
    sched_iso = None
    if sched:
        sched_iso = db.iso(datetime.fromisoformat(sched).replace(tzinfo=agent.tz()))
    old_visual = (post["overlay_text"], post["backdrop"], post["title"], post.get("source_image"), post.get("placement"))
    placement = form.get("placement") if form.get("placement") in PLACEMENTS else post.get("placement")
    db.run("UPDATE posts SET overlay_text=?, backdrop=?, title=?, scheduled_at=?, placement=?, updated_at=? WHERE id=?",
           (form.get("overlay_text", ""), form.get("backdrop", "garage"), form.get("subline", ""),
            sched_iso, placement, db.iso(), post_id))
    chosen = set(form.getlist("platforms"))
    for p in config.PLATFORMS:
        t = next((t for t in post["targets"] if t["platform"] == p), None)
        if p in chosen and not t:
            db.run("INSERT INTO post_targets(post_id, platform, status) VALUES(?, ?, 'pending')", (post_id, p))
        elif p not in chosen and t and t["status"] in ("pending", "failed"):
            db.run("DELETE FROM post_targets WHERE id=?", (t["id"],))
    for t in db.q("SELECT * FROM post_targets WHERE post_id=?", (post_id,)):
        if f"caption_{t['platform']}" in form:
            db.run("UPDATE post_targets SET caption=?, title=? WHERE id=?",
                   (form.get(f"caption_{t['platform']}"), form.get(f"title_{t['platform']}", t["title"]), t["id"]))
    post = agent.get_post(post_id)
    new_visual = (post["overlay_text"], post["backdrop"], post["title"], post.get("source_image"), post.get("placement"))
    needs_media = new_visual != old_visual or any(not t.get("media_path") for t in post["targets"])
    if needs_media:
        agent.render_media(post)
    # edits to an auto-approved post put it back in your hands
    if post["status"] == "approved" and post["approval"] == "auto":
        db.run("UPDATE posts SET status='needs_approval', approval=NULL WHERE id=?", (post_id,))
    action = form.get("action", "save")
    if action == "approve":
        return post_approve(post_id)
    return back(f"/posts/{post_id}", "Saved." + (" Graphics re-rendered." if needs_media else ""))


@app.post("/posts/{post_id}/approve")
def post_approve(post_id: int):
    post = agent.get_post(post_id)
    if not post["scheduled_at"]:
        return back(f"/posts/{post_id}", "Pick a date and time first.")
    db.run("UPDATE posts SET status='approved', approval='manual', updated_at=? WHERE id=?", (db.iso(), post_id))
    return back("/", f"Approved — goes out {fmt_local(post['scheduled_at'])}.")


@app.post("/posts/{post_id}/unapprove")
def post_unapprove(post_id: int):
    db.run("UPDATE posts SET status='needs_approval', approval=NULL WHERE id=? AND status='approved'", (post_id,))
    return back(f"/posts/{post_id}", "Moved back to drafts awaiting your OK.")


@app.post("/posts/{post_id}/reject")
def post_reject(post_id: int):
    db.run("UPDATE posts SET status='rejected', updated_at=? WHERE id=?", (db.iso(), post_id))
    return back("/", "Rejected. It won't be posted.")


@app.post("/posts/{post_id}/delete")
def post_delete(post_id: int):
    agent.delete_post(post_id)
    return back("/calendar", "Post deleted.")


@app.post("/posts/{post_id}/redraft")
def post_redraft(post_id: int):
    run_bg(f"redraft{post_id}", f"Rewriting post #{post_id}", agent.build_post, post_id, True)
    return back(f"/posts/{post_id}", "Claude is rewriting the captions… refresh in ~20 seconds.")


@app.post("/posts/{post_id}/publish-now")
def post_publish_now(post_id: int, platform: str = Form("")):
    run_bg(f"pub{post_id}", f"Publishing post #{post_id}", agent.publish_post, post_id, platform or None)
    return back(f"/posts/{post_id}", "Publishing now…")


@app.post("/posts/{post_id}/retry")
def post_retry(post_id: int):
    db.run("UPDATE post_targets SET status='pending', error=NULL WHERE post_id=? AND status='failed'", (post_id,))
    run_bg(f"pub{post_id}", f"Retrying post #{post_id}", agent.publish_post, post_id)
    return back(f"/posts/{post_id}", "Retrying the failed platforms…")


@app.post("/posts/{post_id}/tiktok-done")
def post_tiktok_done(post_id: int):
    db.run("UPDATE post_targets SET status='published' WHERE post_id=? AND platform='tiktok' AND status='sent_to_inbox'",
           (post_id,))
    agent._rollup_status(post_id)
    return back("/", "Marked the TikTok draft as posted.")


@app.post("/posts/new")
def post_new(kind: str = Form(...), product_id: str = Form(""), review_id: str = Form(""),
             backdrop: str = Form(""), headline: str = Form(""), source_image: str = Form(""),
             when: str = Form(""), photo_id: str = Form(""), placement: str = Form("")):
    sched = None
    if when:
        sched = datetime.fromisoformat(when).replace(tzinfo=agent.tz())
    else:
        sched = (agent.local_now() + timedelta(days=1)).replace(hour=18, minute=30, second=0, microsecond=0)
    if not backdrop:
        notes = {}
        if review_id:
            r = db.q1("SELECT photo_notes FROM reviews WHERE id=?", (review_id,))
            notes = db.jl(r and r["photo_notes"], {}) or {}
        backdrop = notes.get("backdrop") if notes.get("backdrop") in backdrops.list_styles() else "garage"
    if photo_id and not headline:
        ph = photos.get(photo_id)
        headline = ((ph or {}).get("n") or {}).get("headline_idea") or ""
    post_id = agent.create_post(kind=kind, product_id=product_id or None, review_id=review_id or None,
                                angle="", headline=headline, backdrop=backdrop, scheduled_at=sched,
                                created_by="manual", source_image=source_image or None,
                                photo_id=photo_id or None, placement=placement if placement in PLACEMENTS else None)
    run_bg(f"redraft{post_id}", f"Drafting post #{post_id}", agent.build_post, post_id, True)
    return back(f"/posts/{post_id}", "Drafting captions and graphics… refresh in ~20 seconds.")


# ---------------------------------------------------------------- reviews / products
@app.get("/reviews", response_class=HTMLResponse)
def reviews_page(request: Request, show: str = "unused"):
    where = "hidden = 0"
    if show == "unused":
        where += " AND used_at IS NULL"
    elif show == "photos":
        where += " AND image_url IS NOT NULL AND image_url != ''"
    elif show == "hidden":
        where = "hidden = 1"
    rows = db.q(f"SELECT * FROM reviews WHERE {where} ORDER BY COALESCE(photo_score,0) DESC, created_at DESC LIMIT 200")
    for r in rows:
        r["notes"] = db.jl(r.get("photo_notes"), {}) or {}
    return templates.TemplateResponse(request, "reviews.html", ctx(request, nav="reviews", reviews=rows, show=show,
                                                          etsy_ok=etsy.configured()))


@app.post("/reviews/{rid:path}/hide")
def review_hide(rid: str, hide: str = Form("1")):
    db.run("UPDATE reviews SET hidden=? WHERE id=?", (1 if hide == "1" else 0, rid))
    return back("/reviews", "Hidden — the planner won't use it." if hide == "1" else "Restored.")


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request, q: str = ""):
    rows = db.q("SELECT * FROM products WHERE status='active' AND (? = '' OR title LIKE ?) "
                "ORDER BY COALESCE(last_featured_at,'0') ASC, title", (q, f"%{q}%"))
    return templates.TemplateResponse(request, "products.html", ctx(request, nav="products", products=rows, q=q))


# ---------------------------------------------------------------- studio
PLACEMENTS = {"auto": "Auto", "cutout": "Cut out — new background", "full": "Full photo (fills the frame)",
              "framed": "Framed photo"}


@app.get("/studio", response_class=HTMLResponse)
def studio(request: Request, product_id: str = "", review_id: str = "", photo_id: str = ""):
    photos = []
    for r in db.q("SELECT * FROM photos WHERE hidden=0 AND (score IS NULL OR score >= 5) "
                  "ORDER BY source='edit' DESC, COALESCE(score,0) DESC, taken_at DESC LIMIT 150"):
        n = db.jl(r["notes"], {}) or {}
        label = (f"★{r['score']} " if r["score"] else "") + (n.get("description") or r["filename"] or "")[:60]
        photos.append({"src": str(config.DATA_DIR / r["local_path"]), "label": label,
                        "group": "New backgrounds" if r["source"] == "edit" else "Your photos",
                        "placement": n.get("placement") or "", "backdrop": n.get("suggested_backdrop") or ""})
    for p in db.q("SELECT id, title, images FROM products WHERE status='active' ORDER BY title LIMIT 200"):
        for i, u in enumerate(db.jl(p["images"])[:4]):
            photos.append({"src": u, "label": p["title"][:60] + (f" ({i + 1})" if i else ""), "group": "Products"})
    for r in db.q("SELECT id, text, image_url FROM reviews WHERE image_url IS NOT NULL AND image_url != '' AND hidden=0 LIMIT 100"):
        photos.append({"src": r["image_url"], "label": "Review: " + (r["text"] or "")[:50], "group": "Review photos"})
    preselect, pre_layout = "", ""
    if photo_id:
        ph = photos_mod.get(photo_id)
        preselect = str(config.DATA_DIR / ph["local_path"]) if ph else ""
    elif product_id:
        p = db.q1("SELECT images FROM products WHERE id = ?", (product_id,))
        imgs = db.jl(p["images"]) if p else []
        preselect = imgs[0] if imgs else ""
    elif review_id:
        r = db.q1("SELECT image_url FROM reviews WHERE id = ?", (review_id,))
        preselect, pre_layout = (r["image_url"] if r else "") or "", "review_photo"
    if not preselect and photos:
        preselect = photos[0]["src"]
    return templates.TemplateResponse(request, "studio.html", ctx(request, nav="studio", photos=photos,
                                                         styles=backdrops.list_styles(), formats=compose.FORMATS,
                                                         preselect=preselect, preselect_layout=pre_layout,
                                                         placements=PLACEMENTS, bg_options=bgswap.options(),
                                                         ai_bg=bgswap.ai_available()))


@app.get("/studio/preview")
def studio_preview(fmt: str = "portrait", photo: str = "", backdrop: str = "garage", headline: str = "",
                   subline: str = "", layout: str = "product", placement: str = "auto", review_text: str = "",
                   rating: int = 5):
    if fmt not in compose.FORMATS:
        raise HTTPException(400)
    spec = {"layout": layout, "photo": photo or None, "backdrop": backdrop, "headline": headline,
            "subline": subline, "placement": placement, "review_text": review_text or None, "rating": rating}
    return Response(compose.preview_bytes(spec, fmt), media_type="image/jpeg")


@app.post("/studio/upload")
async def studio_upload(file: UploadFile = File(...), kind: str = Form("photo")):
    target = backdrops.CUSTOM_DIR if kind == "backdrop" else config.DATA_DIR / "uploads"
    target.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "upload.jpg").name.replace(" ", "_")
    (target / name).write_bytes(await file.read())
    if kind != "backdrop":  # uploads join your photo library, so they can get new backgrounds too
        pid = photos_mod.add_file(target / name)
        if pid:
            return back(f"/studio?photo_id={quote(pid)}", f"Uploaded {name}.")
    return back("/studio", f"Uploaded {name}" + (" as a backdrop." if kind == "backdrop" else "."))


@app.post("/studio/save")
def studio_save(photo: str = Form(""), backdrop: str = Form("garage"), headline: str = Form(""),
                kind: str = Form("product_spotlight"), when: str = Form(""), placement: str = Form("auto")):
    product_id = None
    review_id = None
    lib = photos_mod.by_path(photo) if photo else None
    if lib:
        return post_new(kind="showcase" if kind == "product_spotlight" else kind, product_id="", review_id="",
                        backdrop=backdrop, headline=headline, source_image="", when=when, photo_id=lib["id"],
                        placement=placement if placement != "auto" else "")
    if photo:
        for p in db.q("SELECT id, images FROM products"):
            if photo in db.jl(p["images"]):
                product_id = p["id"]
                break
        r = db.q1("SELECT id FROM reviews WHERE image_url = ?", (photo,))
        if r:
            review_id = r["id"]
            kind = "review_photo"
    return post_new(kind=kind, product_id=product_id or "", review_id=review_id or "", backdrop=backdrop,
                    headline=headline, source_image=photo, when=when, photo_id="",
                    placement=placement if placement != "auto" else "")


@app.post("/studio/background")
def studio_background(photo: str = Form(...), choice: str = Form(...), custom: str = Form("")):
    lib = photos_mod.by_path(photo)
    if not lib:
        return back("/studio", "New backgrounds work on photos from your library (iCloud / folders / uploads "
                               "aren't in it yet). Pick one under “Your photos”.")
    return photo_background(lib["id"], choice=choice, custom=custom, then=f"/studio?photo_id={quote(lib['id'])}")


# ---------------------------------------------------------------- photo library
PHOTO_VIEWS = [("best", "Best picks"), ("new_bg", "Needs a new background"), ("all", "All"),
               ("unrated", "Not rated yet"), ("edits", "New backgrounds"), ("used", "Used"), ("hidden", "Hidden")]


@app.get("/photos", response_class=HTMLResponse)
def photos_page(request: Request, show: str = "best", tags: str = ""):
    photos_mod.backfill_tags()
    want = photos_mod.clean_tags(tags.split(","))
    where = {"best": "hidden=0 AND score >= 7 AND source != 'edit'",
             "new_bg": "hidden=0 AND json_extract(notes, '$.background') = 'mundane' AND source != 'edit'",
             "all": "hidden=0 AND source != 'edit'", "unrated": "hidden=0 AND score IS NULL",
             "edits": "hidden=0 AND source = 'edit'", "used": "used_at IS NOT NULL",
             "hidden": "hidden=1"}.get(show, "hidden=0")
    # every chosen tag must be on the photo
    where += "".join(" AND EXISTS (SELECT 1 FROM json_each(photos.tags) WHERE value = ?)" for _ in want)
    rows = db.q(f"SELECT * FROM photos WHERE {where} ORDER BY "
                + ("created_at DESC" if show == "edits" else "COALESCE(score,0) DESC, taken_at DESC") + " LIMIT 240",
                tuple(want))
    for r in rows:
        r["n"] = db.jl(r.get("notes"), {}) or {}
        r["tag_list"] = photos_mod.tags_of(r)
    counts = {"total": db.q1("SELECT COUNT(*) n FROM photos WHERE source != 'edit'")["n"],
              "unrated": db.q1("SELECT COUNT(*) n FROM photos WHERE score IS NULL AND hidden=0")["n"]}
    return templates.TemplateResponse(request, "photos.html", ctx(
        request, nav="photos", photos=rows, show=show, views=PHOTO_VIEWS, counts=counts,
        want=want, all_tags=photos_mod.all_tags(),
        ic=icloud.status(), folders=photos_mod.folders(), auto_folder=photos_mod.icloud_windows_folder(),
        bg_options=bgswap.options(), ai_bg=bgswap.ai_available(), can_cut=compose.can_cut_out(),
        showcase_kinds={k: v for k, v in config.POST_KINDS.items() if k not in agent.REVIEW_KINDS}))


@app.post("/photos/scan")
def photos_scan():
    run_bg("photos", "Scanning your photos", agent.sync_photos)
    return back("/photos", "Looking for new photos — Claude rates them as they come in.")


@app.post("/photos/settings")
def photos_settings(album: str = Form(""), days: int = Form(90), max_per_scan: int = Form(150),
                    rate_per_scan: int = Form(36), folders: str = Form("")):
    db.set_setting("photos_album", album.strip())
    db.set_setting("photos_days", max(1, days))
    db.set_setting("photos_max_per_scan", max(1, min(max_per_scan, 1000)))
    db.set_setting("photos_rate_per_scan", max(0, min(rate_per_scan, 300)))
    db.set_setting("photos_folders", [f.strip() for f in folders.splitlines() if f.strip()])
    return back("/photos", "Photo settings saved.")


@app.post("/photos/icloud/login")
def icloud_login(apple_id: str = Form(...), password: str = Form(...)):
    try:
        res = icloud.start_login(apple_id, password)
    except Exception as e:
        return back("/photos", f"iCloud login failed: {e}")
    if res == "code":
        return back("/photos", "Apple sent a 6-digit code to your iPhone / Mac — enter it below.")
    run_bg("photos", "Scanning your photos", agent.sync_photos)
    return back("/photos", "iCloud connected. Scanning your photos…")


@app.post("/photos/icloud/code")
def icloud_code(code: str = Form(...)):
    try:
        ok = icloud.finish_login(code)
    except Exception as e:
        return back("/photos", f"iCloud: {e}")
    if not ok:
        if icloud.waiting_for_code():
            return back("/photos", "Apple didn't accept that code, so it sent a new one — enter the newest code.")
        return back("/photos", "Apple didn't accept that code. Enter your password again to get a new one.")
    run_bg("photos", "Scanning your photos", agent.sync_photos)
    return back("/photos", "iCloud connected. Scanning your photos…")


@app.post("/photos/icloud/resend")
def icloud_resend():
    try:
        res = icloud.resend_code()
    except Exception as e:
        icloud.cancel_login()
        return back("/photos", f"iCloud: {e}")
    if res == "ok":
        run_bg("photos", "Scanning your photos", agent.sync_photos)
        return back("/photos", "iCloud connected. Scanning your photos…")
    return back("/photos", "Apple is sending a new code — use the newest one.")


@app.post("/photos/icloud/cancel")
def icloud_cancel():
    icloud.cancel_login()
    return back("/photos", "Login cancelled — start again whenever you're ready.")


@app.post("/photos/icloud/logout")
def icloud_logout():
    icloud.logout()
    return back("/photos", "Disconnected iCloud. Photos already pulled in stay here.")


@app.post("/photos/bulk")
async def photos_bulk(request: Request):
    form = await request.form()
    ids = form.getlist("ids")
    show = form.get("show") or "best"
    keep = f"/photos?show={quote(show)}" + (f"&tags={quote(form.get('tags'))}" if form.get("tags") else "")
    if form.get("action") == "tag":
        tag = (form.get("tag") or "").strip()
        if not tag or not ids:
            return back(keep, "Pick some photos and type a tag first.")
        n = photos_mod.add_tag(ids, tag)
        return back(keep, f"Tagged {n} photo{'s' if n != 1 else ''} “{photos_mod.clean_tags([tag])[0]}”.")
    hide = 1 if form.get("action") == "hide" else 0
    with db.tx() as c:
        c.executemany("UPDATE photos SET hidden=? WHERE id=?", [(hide, i) for i in ids])
    n = len(ids)
    return back(keep, f"{'Hid' if hide else 'Restored'} {n} photo{'s' if n != 1 else ''}."
                + (" The planner won't use them." if hide else ""))


@app.post("/photos/{pid:path}/tags")
def photo_tags(pid: str, tags: str = Form("[]")):
    """Replace a photo's tags (the cards save edits here without reloading the page)."""
    if not photos_mod.get(pid):
        raise HTTPException(404)
    try:
        lst = json.loads(tags)
    except ValueError:
        lst = tags.split(",")
    return JSONResponse({"tags": photos_mod.set_tags(pid, lst if isinstance(lst, list) else [])})


@app.post("/photos/{pid:path}/hide")
def photo_hide(pid: str, hide: str = Form("1")):
    db.run("UPDATE photos SET hidden=? WHERE id=?", (1 if hide == "1" else 0, pid))
    return back(f"/photos?show={'hidden' if hide != '1' else 'best'}",
                "Hidden — the planner won't use it." if hide == "1" else "Restored.")


@app.post("/photos/{pid:path}/background")
def photo_background(pid: str, choice: str = Form(...), custom: str = Form(""), then: str = Form("/photos?show=edits")):
    if choice == "scene:custom":
        if not custom.strip():
            return back(then, "Describe the scene you want first.")
        choice = "scene:" + custom.strip()[:300]
    if not photos_mod.get(pid):
        raise HTTPException(404)
    if choice.startswith("scene:") and not bgswap.ai_available():
        return back(then, "AI scenes need OPENAI_API_KEY in .env (and background removal installed).")
    run_bg(f"bg{pid}", "Making a new background", bgswap.replace_background, pid, choice)
    note = "about a minute" if choice.startswith("scene:") else "a few seconds"
    return back("/photos?show=edits" if then.startswith("/photos") else then,
                f"Making the new background — takes {note}. It shows up under “New backgrounds”.")


@app.post("/photos/{pid:path}/post")
def photo_post(pid: str, kind: str = Form("showcase"), placement: str = Form("")):
    if not photos_mod.get(pid):
        raise HTTPException(404)
    return post_new(kind=kind if kind in config.POST_KINDS and kind not in agent.REVIEW_KINDS else "showcase",
                    product_id="", review_id="", backdrop="", headline="", source_image="", when="",
                    photo_id=pid, placement=placement)


# ---------------------------------------------------------------- insights
@app.get("/insights", response_class=HTMLResponse)
def insights(request: Request, days: int = 28):
    rows = agent.insights_rows(days)
    by_platform: dict[str, dict] = {}
    by_kind: dict[str, dict] = {}
    for r in rows:
        for key, bucket in ((r["platform"], by_platform), (r["kind"], by_kind)):
            b = bucket.setdefault(key, {"posts": 0, "engagement": 0, "impressions": 0})
            b["posts"] += 1
            b["engagement"] += r["engagement"] or 0
            b["impressions"] += (r["impressions"] or r["views"] or r["reach"] or 0)
    rep = db.q1("SELECT * FROM reports WHERE kind='weekly_performance' ORDER BY id DESC LIMIT 1")
    return templates.TemplateResponse(request, "insights.html", ctx(
        request, nav="insights", rows=rows, by_platform=by_platform, by_kind=by_kind, days=days,
        report=json.loads(rep["body"]) if rep else None, report_at=rep["created_at"] if rep else None))


# ---------------------------------------------------------------- settings
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    boards, board_err = [], ""
    pin = REGISTRY["pinterest"]
    if pin.connected():
        try:
            boards = pin.boards()
        except Exception as e:
            board_err = str(e)
    meta = db.get_token("meta") or {}
    return templates.TemplateResponse(request, "settings.html", ctx(
        request, nav="settings", conns=connections(), boards=boards, board_err=board_err, meta=meta,
        shopify_ok=shopify.configured(), etsy_ok=etsy.configured(), redirect=oauth_mod.redirect_uri,
        ic=icloud.status(check=False),
        day_keys=agent.DAY_KEYS))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    section = form.get("section")
    if section == "safety":
        db.set_setting("dry_run", form.get("dry_run") == "on")
        db.set_setting("posting_paused", form.get("posting_paused") == "on")
        db.set_setting("hold_hours", float(form.get("hold_hours") or 0))
        db.set_setting("auto_approve_kinds", [k for k in form.getlist("auto_approve_kinds")
                                              if k not in agent.REVIEW_KINDS])
    elif section == "platforms":
        db.set_setting("enabled_platforms", [p for p in config.PLATFORMS if form.get(f"enable_{p}") == "on"])
        db.set_setting("tiktok_mode", form.get("tiktok_mode", "inbox"))
        db.set_setting("x_allow_links", form.get("x_allow_links") == "on")
        if form.get("pinterest_board_id") is not None:
            db.set_setting("pinterest_board_id", form.get("pinterest_board_id", ""))
    elif section == "cadence":
        db.set_setting("posts_per_week", int(form.get("posts_per_week") or 5))
        db.set_setting("review_posts_per_week", int(form.get("review_posts_per_week") or 2))
        db.set_setting("min_review_rating", int(form.get("min_review_rating") or 5))
        db.set_setting("posting_times", {d: form.get(f"time_{d}") or "18:00" for d in agent.DAY_KEYS})
        db.set_setting("planning_day", form.get("planning_day", "sun"))
        db.set_setting("planning_time", form.get("planning_time", "16:00"))
        db.set_setting("timezone", form.get("timezone") or "America/New_York")
        scheduler.reschedule_weekly()
    elif section == "brand":
        db.set_setting("brand_name", form.get("brand_name", ""))
        db.set_setting("brand_voice", form.get("brand_voice", ""))
        db.set_setting("brand_hashtags", [h.strip() if h.strip().startswith("#") else "#" + h.strip()
                                          for h in (form.get("brand_hashtags") or "").replace(",", " ").split() if h.strip()])
        db.set_setting("avoid", form.get("avoid", ""))
        db.set_setting("accent_color", form.get("accent_color", "#1E88E5"))
        db.set_setting("accent_color_2", form.get("accent_color_2", "#FF3D7F"))
        db.set_setting("watermark", form.get("watermark", ""))
    elif section == "meta":
        old = db.get_token("meta") or {}
        data = {"page_id": form.get("page_id", "").strip(), "ig_user_id": form.get("ig_user_id", "").strip(),
                "access_token": form.get("access_token", "").strip() or old.get("access_token", "")}
        db.save_token("meta", data)
        db.delete_token("meta_page")
    return back("/settings", "Settings saved.")


@app.post("/settings/meta/test")
def meta_test():
    from .publishers.meta import _creds, _g
    from .publishers.base import request as req
    c = _creds()
    r = req("GET", _g("me"), params={"access_token": c["token"], "fields": "id,name"})
    msg = f"Meta token OK: {r.json().get('name')}" if r.status_code == 200 else f"Meta said: {r.text[:200]}"
    if r.status_code == 200 and c["ig_user_id"]:
        r2 = req("GET", _g(c["ig_user_id"]), params={"access_token": c["token"], "fields": "username"})
        msg += f" · Instagram: @{r2.json().get('username')}" if r2.status_code == 200 else f" · Instagram error: {r2.text[:160]}"
    return back("/settings", msg)


@app.post("/settings/disconnect/{platform}")
def disconnect(platform: str):
    db.delete_token(platform)
    return back("/settings", f"Disconnected {config.PLATFORM_LABELS.get(platform, platform)}.")


@app.post("/settings/test-shopify-upload")
def test_shopify_upload():
    try:
        p = config.CACHE_DIR / "upload_test.jpg"
        compose.render_to_file({"layout": "product", "backdrop": "racing", "headline": "Upload test"}, "square", p)
        url = shopify.upload_image(p, alt="Upload test")
        return back("/settings", f"Shopify Files upload works: {url}")
    except Exception as e:
        return back("/settings", f"Shopify upload failed: {e}")


# ---------------------------------------------------------------- oauth
@app.get("/oauth/{platform}/start")
def oauth_start(platform: str):
    pub = REGISTRY.get(platform)
    if not pub or not hasattr(pub, "auth_url"):
        raise HTTPException(404)
    if not pub.configured():
        return back("/settings", f"Add the {config.PLATFORM_LABELS[platform]} app keys to .env first.")
    return RedirectResponse(pub.auth_url())


@app.get("/oauth/{platform}/callback")
def oauth_callback(platform: str, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return back("/settings", f"{platform} said no: {error_description or error}")
    st = oauth_mod.pop_state(state)
    if not st or st.get("platform") != platform:
        return back("/settings", "That login link expired — try Connect again.")
    pub = REGISTRY[platform]
    try:
        if platform == "x":
            pub.exchange(code, st["verifier"])
        else:
            pub.exchange(code)
    except Exception as e:
        return back("/settings", f"Couldn't finish connecting {platform}: {e}")
    db.log(f"Connected {platform}")
    return back("/settings", f"{config.PLATFORM_LABELS[platform]} connected.")
