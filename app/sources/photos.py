"""Your own photo library: iCloud Photos plus any local folders (iCloud for Windows' synced folder is
picked up on its own). Scanning keeps a preview-size JPEG of each new photo under data/photos/ for Claude
to rate and for the Studio to use; the full-resolution original is fetched only when a photo becomes a post.
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .. import config, db
from . import icloud

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

PHOTO_DIR = config.DATA_DIR / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp"}
PREVIEW_MAX = 1600
ORIGINAL_MAX = 3000


def _rel(p: Path) -> str:
    return str(p.relative_to(config.DATA_DIR))


def _key(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _save_jpeg(img: Image.Image, dest: Path, max_side: int) -> tuple[int, int]:
    """Save an upright RGB JPEG no bigger than max_side; returns the source's upright size."""
    img = ImageOps.exif_transpose(img)
    size = img.size
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=90, optimize=True)
    return size


def _insert(pid: str, source: str, filename: str, album: str, taken: datetime | None, size, local: Path,
            original: str | None = None, parent_id: str | None = None, score=None, notes: str | None = None) -> None:
    db.run("""INSERT OR IGNORE INTO photos(id, source, filename, album, taken_at, width, height, local_path,
              original_path, parent_id, score, notes, rated_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
           (pid, source, filename, album, db.iso(taken) if taken else None, size[0], size[1], _rel(local),
            original, parent_id, score, notes, db.iso() if score is not None else None, db.iso()))


def _looks_like_screenshot(filename: str) -> bool:
    # iPhone screenshots are IMG_1234.PNG; camera shots are .HEIC/.JPG. Skipping them saves rating money.
    f = filename.lower()
    return f.endswith(".png") and (f.startswith("img_") or "screenshot" in f or "screen shot" in f)


# ------------------------------------------------------------------ folders
def icloud_windows_folder() -> Path | None:
    home = Path.home()
    for p in (home / "Pictures" / "iCloud Photos" / "Photos", home / "Pictures" / "iCloud Photos"):
        if p.is_dir():
            return p
    return None


UPLOADS_DIR = config.DATA_DIR / "uploads"


def folders() -> list[Path]:
    out = [Path(f).expanduser() for f in (db.get_setting("photos_folders") or []) if str(f).strip()]
    for auto in (icloud_windows_folder(), UPLOADS_DIR):
        if auto and auto not in out:
            out.append(auto)
    return [p for p in out if p.is_dir()]


def add_file(p: Path) -> str | None:
    """Add one local image (e.g. a Studio upload) to the library right away. Returns its id."""
    pid = f"folder:{_key(str(p))}"
    if get(pid):
        return pid
    dest = PHOTO_DIR / f"{pid.replace(':', '_')}.jpg"
    try:
        with Image.open(p) as img:
            taken = _exif_date(img) or datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
            size = _save_jpeg(img, dest, PREVIEW_MAX)
    except Exception as e:
        db.log(f"Photos: couldn't add {p.name}: {e}", "warn")
        return None
    _insert(pid, "folder", p.name, p.parent.name, taken, size, dest, original=str(p))
    return pid


def scan_folders(cutoff: datetime, max_new: int) -> int:
    known = {r["original_path"] for r in db.q("SELECT original_path FROM photos WHERE source='folder'")}
    files = []
    for folder in folders():
        for p in folder.rglob("*"):
            if p.suffix.lower() not in IMAGE_EXT or _looks_like_screenshot(p.name) or str(p) in known:
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                files.append((mtime, p))
    return sum(1 for _, p in sorted(files, reverse=True)[:max_new] if add_file(p))


def _exif_date(img: Image.Image) -> datetime | None:
    try:
        raw = img.getexif().get_ifd(0x8769).get(36867) or img.getexif().get(306)  # DateTimeOriginal / DateTime
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc) if raw else None
    except Exception:
        return None


# ------------------------------------------------------------------- iCloud
def _newest_first(album, page_size: int = 100):
    """The whole library, newest taken first. iCloud's library index has the newest photo at position 0;
    pyicloud's own iterator starts from the far end (oldest first), and its "recently added" list stops
    after a few hundred items, so page through the index ourselves."""
    from pyicloud.services.photos import DirectionEnum
    offset, seen = 0, set()
    while True:
        page = list(album._get_photos_at(offset, DirectionEnum.ASCENDING, page_size))
        for p in page:
            if p.id not in seen:
                seen.add(p.id)
                yield p
        if len(page) < page_size // 2:
            return
        offset += len(page)


def _album(svc, name: str):
    """(album, newest_first). Blank name = the whole library."""
    if not name:
        return svc.photos.all, True
    album = svc.photos.albums.find(name)
    if album is None:
        try:
            album = svc.photos.shared_streams.find(name)
        except Exception:
            album = None
    if album is None:
        raise RuntimeError(f"No iCloud album called “{name}”")
    return album, False


def scan_icloud(cutoff: datetime, max_new: int) -> int:
    svc = icloud.api()
    album_name = (db.get_setting("photos_album") or "").strip()
    try:
        album, newest_first = _album(svc, album_name)
        known = {r["id"] for r in db.q("SELECT id FROM photos WHERE source='icloud'")}
        n, older_in_a_row = 0, 0
        for i, asset in enumerate(_newest_first(album) if newest_first else album.photos):
            if i >= 20000:
                break
            taken = asset.asset_date
            if taken.tzinfo is None:
                taken = taken.replace(tzinfo=timezone.utc)
            # The whole library comes newest first, so stop once we're past the cut-off.
            # A named album is something you curated, so take all of it.
            if newest_first and taken < cutoff:
                older_in_a_row += 1
                if older_in_a_row >= 25:
                    break
                continue
            older_in_a_row = 0
            pid = f"icloud:{asset.id}"
            if pid in known or asset.item_type != "image" or _looks_like_screenshot(asset.filename or ""):
                continue
            data = asset.download("medium") or asset.download("original")
            if not data:
                continue
            dest = PHOTO_DIR / f"icloud_{_key(asset.id)}.jpg"
            try:
                with Image.open(io.BytesIO(data)) as img:
                    _save_jpeg(img, dest, PREVIEW_MAX)
            except Exception as e:
                db.log(f"Photos: couldn't read iCloud photo {asset.filename}: {e}", "warn")
                continue
            w, h = asset.dimensions
            _insert(pid, "icloud", asset.filename, album_name, taken, (w or 0, h or 0), dest)
            known.add(pid)
            n += 1
            if n >= max_new:
                break
        return n
    except icloud.NeedsLogin:
        raise
    except Exception as e:
        if "401" in str(e) or "auth" in type(e).__name__.lower():
            icloud.drop_session()
        raise


def scan() -> dict:
    s = db.all_settings()
    cutoff = db.utcnow() - timedelta(days=int(s.get("photos_days") or 90))
    max_new = int(s.get("photos_max_per_scan") or 150)
    out = {"icloud": 0, "folders": 0, "errors": []}
    if icloud.configured():
        try:
            out["icloud"] = scan_icloud(cutoff, max_new)
        except Exception as e:
            out["errors"].append(f"iCloud: {e}")
            db.log(f"iCloud photo scan failed: {e}", "warn" if isinstance(e, icloud.NeedsLogin) else "error")
    try:
        out["folders"] = scan_folders(cutoff, max_new)
    except Exception as e:
        out["errors"].append(f"folders: {e}")
        db.log(f"Folder photo scan failed: {e}", "error")
    db.log(f"Photos scan: {out['icloud']} new from iCloud, {out['folders']} from folders"
           + (f" ({'; '.join(out['errors'])})" if out["errors"] else ""))
    return out


# --------------------------------------------------------------------- tags
SUBJECT_TAGS = {"truck_with_product": ["truck", "our product"], "product_closeup": ["close-up", "our product"],
                "truck": ["truck"], "making_or_shop": ["shop"], "people": ["people"], "other": []}


def clean_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = " ".join(str(t).replace(",", " ").strip().lower().lstrip("#").split())[:40]
        if t and t not in out:
            out.append(t)
    return out[:20]


def default_tags(notes: dict, source: str = "") -> list[str]:
    """Starting tags from Claude's rating; after that the tags are yours to edit."""
    tags = list(notes.get("tags") or [])
    tags += SUBJECT_TAGS.get(notes.get("subject") or "", [])
    if notes.get("shows_product"):
        tags.append("our product")
    if notes.get("background") == "mundane":
        tags.append("dull background")
    if notes.get("privacy_issue"):
        tags.append("privacy check")
    if source == "edit" or notes.get("edit"):
        tags.append("new background")
    return clean_tags(tags)


def tags_of(row: dict) -> list[str]:
    return db.jl(row.get("tags"))


def set_tags(pid: str, tags) -> list[str]:
    tags = clean_tags(tags)
    db.run("UPDATE photos SET tags = ? WHERE id = ?", (json.dumps(tags), pid))
    return tags


def add_tag(pids: list[str], tag: str) -> int:
    tag = (clean_tags([tag]) or [""])[0]
    if not tag:
        return 0
    for row in db.q(f"SELECT id, tags FROM photos WHERE id IN ({','.join('?' * len(pids))})", tuple(pids)):
        set_tags(row["id"], tags_of(row) + [tag])
    return len(pids)


def all_tags() -> list[tuple[str, int]]:
    """Every tag in use (on photos you haven't hidden), most used first."""
    return [(r["value"], r["n"]) for r in db.q(
        "SELECT j.value, COUNT(*) n FROM photos, json_each(photos.tags) j WHERE photos.hidden = 0 "
        "GROUP BY j.value ORDER BY n DESC, j.value")]


def backfill_tags() -> None:
    """Give photos that were rated before tags existed their starting tags (one time)."""
    for r in db.q("SELECT id, source, notes FROM photos WHERE tags IS NULL AND notes IS NOT NULL"):
        set_tags(r["id"], default_tags(db.jl(r["notes"], {}) or {}, r["source"]))


# ------------------------------------------------------------- using photos
def get(pid: str) -> dict | None:
    row = db.q1("SELECT * FROM photos WHERE id = ?", (pid,))
    if row:
        row["n"] = db.jl(row.get("notes"), {}) or {}
    return row


def by_path(path: str) -> dict | None:
    """The library photo whose working or original file this is (the Studio passes paths around)."""
    p = Path(path)
    try:
        rel = str(p.relative_to(config.DATA_DIR)) if p.is_absolute() else str(p)
    except ValueError:
        rel = str(p)
    row = db.q1("SELECT id FROM photos WHERE local_path = ? OR original_path = ? OR original_path = ?",
                (rel, path, rel))
    return get(row["id"]) if row else None


def best_image(pid: str, fetch: bool = True) -> str:
    """Path of the best copy of a photo for rendering a post: the original (fetched from iCloud once,
    if possible — skipped with fetch=False), otherwise the preview."""
    row = get(pid)
    if not row:
        raise KeyError(pid)
    if row["source"] == "folder" and row.get("original_path") and Path(row["original_path"]).exists():
        return row["original_path"]
    if row["source"] == "icloud":
        orig = row.get("original_path")
        if orig and (config.DATA_DIR / orig).exists():
            return str(config.DATA_DIR / orig)
        if not fetch:
            return str(config.DATA_DIR / row["local_path"])
        try:
            svc = icloud.api()
            album, _ = _album(svc, row.get("album") or "")
            asset = album.get(row["id"].split(":", 1)[1])
            data = asset.download("original") if asset else None
            if data:
                dest = PHOTO_DIR / "originals" / f"icloud_{_key(row['id'])}.jpg"
                with Image.open(io.BytesIO(data)) as img:
                    _save_jpeg(img, dest, ORIGINAL_MAX)
                db.run("UPDATE photos SET original_path = ? WHERE id = ?", (_rel(dest), pid))
                return str(dest)
        except Exception as e:
            db.log(f"Photos: using the preview of {row['filename']} (couldn't get the original: {e})", "warn")
    return str(config.DATA_DIR / row["local_path"])


def add_edit(parent: dict, img: Image.Image, label: str) -> str:
    """Save an edited version (e.g. new background) as its own library photo."""
    pid = f"edit:{_key(parent['id'] + label + db.iso())}"
    dest = PHOTO_DIR / f"{pid.replace(':', '_')}.jpg"
    size = _save_jpeg(img, dest, ORIGINAL_MAX)
    notes = dict(parent.get("n") or {})
    notes.update({"edit": label, "background": "great", "placement": "full",
                  "description": (notes.get("description") or "Photo") + f" — new background: {label}"})
    _insert(pid, "edit", f"{Path(parent.get('filename') or 'photo').stem} ({label[:40]}).jpg",
            parent.get("album") or "", db.parse_iso(parent.get("taken_at")), size, dest,
            original=_rel(dest), parent_id=parent["id"], score=parent.get("score"), notes=json.dumps(notes))
    set_tags(pid, tags_of(parent) + ["new background"])
    return pid
