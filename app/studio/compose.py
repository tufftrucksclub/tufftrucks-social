"""Renders post graphics: product shots, review photos on new backdrops, and
review quote cards, in every size each platform wants."""
from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .. import config, db
from . import backdrops

try:  # iPhone photos (.heic) — some Shopify product images are HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

FONT_DIR = Path(__file__).parent / "fonts"
HEADLINE_FONT = FONT_DIR / "Anton-Regular.ttf"
BODY_FONT = FONT_DIR / "Oswald.ttf"

FORMATS = {
    "portrait": (1080, 1350),   # Instagram feed
    "square": (1080, 1080),     # Facebook
    "story": (1080, 1920),      # TikTok / Stories / Reels
    "pin": (1000, 1500),        # Pinterest
    "landscape": (1600, 900),   # X
}
PLATFORM_FORMAT = {"instagram": "portrait", "facebook": "square", "tiktok": "story",
                   "pinterest": "pin", "x": "landscape"}


# ------------------------------------------------------------------ fonts
def font(path: Path, size: int, weight: str | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(path), size)
    if weight:
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
    return f


# ----------------------------------------------------------------- images
def load_image(src: str) -> Image.Image:
    """Open a local path or URL (URLs cached under data/cache)."""
    if src.startswith("http://") or src.startswith("https://"):
        cache = config.CACHE_DIR / f"img_{hashlib.sha1(src.encode()).hexdigest()[:20]}"
        if not cache.exists():
            r = httpx.get(src, timeout=60, follow_redirects=True)
            r.raise_for_status()
            cache.write_bytes(r.content)
        img = Image.open(cache)
    else:
        p = Path(src)
        if not p.is_absolute():
            p = config.DATA_DIR / src
        img = Image.open(p)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")


_rembg_session = None


def cutout(img: Image.Image, cache_key: str) -> Image.Image | None:
    """Remove the background. Returns None if rembg isn't installed or the result
    doesn't look like a clean product cutout."""
    global _rembg_session
    cache = config.CACHE_DIR / f"cut_{hashlib.sha1(cache_key.encode()).hexdigest()[:20]}.png"
    if cache.exists():
        out = Image.open(cache).convert("RGBA")
    else:
        try:
            from rembg import new_session, remove
        except ImportError:
            return None
        if _rembg_session is None:
            _rembg_session = new_session(config.env("REMBG_MODEL", "isnet-general-use"))
        src = img.convert("RGB")
        if max(src.size) > 1600:
            src.thumbnail((1600, 1600), Image.LANCZOS)
        out = remove(src, session=_rembg_session).convert("RGBA")
        out.save(cache)
    alpha = np.asarray(out.getchannel("A"))
    coverage = (alpha > 128).mean()
    if coverage < 0.03 or coverage > 0.92:
        return None
    bbox = out.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    return out.crop(bbox) if bbox else None


# -------------------------------------------------------------- drawing
def _luma(img: Image.Image, box) -> float:
    a = np.asarray(img.crop(box).convert("L"), dtype=float)
    return float(a.mean())


def _fit_text(draw, text, font_path, max_w, max_size, min_size, weight=None, max_lines=2):
    """Largest font size where text wraps into <= max_lines lines within max_w."""
    words = text.split()
    for size in range(max_size, min_size - 1, -2):
        f = font(font_path, size, weight)
        lines, cur = [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=f) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines and all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f, lines
    f = font(font_path, min_size, weight)
    return f, _wrap(draw, text, f, max_w)[:max_lines]


def _wrap(draw, text, f, max_w):
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _shadowed_text(canvas: Image.Image, xy, text, f, fill, shadow=(0, 0, 0, 170), offset=4, anchor="la"):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((xy[0] + offset, xy[1] + offset), text, font=f, fill=shadow, anchor=anchor)
    layer = layer.filter(ImageFilter.GaussianBlur(offset * 0.9))
    ImageDraw.Draw(layer).text(xy, text, font=f, fill=fill, anchor=anchor)
    canvas.alpha_composite(layer)


def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def _star(cx, cy, r):
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return pts


def _place_cutout(canvas: Image.Image, cut: Image.Image, box, floor: bool, glow_color):
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    scale = min(bw / cut.width, bh / cut.height)
    cut = cut.resize((max(1, int(cut.width * scale)), max(1, int(cut.height * scale))), Image.LANCZOS)
    px = x0 + (bw - cut.width) // 2
    py = y0 + (bh - cut.height) // 2 if not floor else y1 - cut.height
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    if floor:  # contact shadow on the floor
        sw = cut.width * 0.9
        sd.ellipse([px + (cut.width - sw) / 2, py + cut.height - cut.height * 0.05,
                    px + (cut.width + sw) / 2, py + cut.height + cut.height * 0.07], fill=(0, 0, 0, 150))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(6, cut.width // 30)))
    else:      # floating drop shadow
        a = cut.getchannel("A").point(lambda v: int(v * 0.55))
        blk = Image.new("RGBA", cut.size, (0, 0, 0, 255))
        blk.putalpha(a)
        shadow.alpha_composite(blk, (px + cut.width // 40, py + cut.height // 25))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(8, cut.width // 25)))
    if glow_color:
        g = Image.new("RGBA", cut.size, glow_color + (255,))
        g.putalpha(cut.getchannel("A").point(lambda v: int(v * 0.6)))
        glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        glow.alpha_composite(g, (px, py))
        canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(18, cut.width // 12))))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(cut, (px, py))


def _place_framed(canvas: Image.Image, photo: Image.Image, box, tilt: float = -2.5):
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    ratio = min(max(photo.width / photo.height, 0.75), 1.5)  # keep frames sane
    fw = bw * 0.92
    fh = fw / ratio
    if fh > bh * 0.95:
        fh = bh * 0.95
        fw = fh * ratio
    border = max(10, int(fw * 0.025))
    inner = ImageOps.fit(photo.convert("RGB"), (int(fw - 2 * border), int(fh - 2 * border)), Image.LANCZOS)
    card = Image.new("RGB", (int(fw), int(fh)), (250, 250, 248))
    card.paste(inner, (border, border))
    card = _rounded(card, border)
    card = card.rotate(tilt, resample=Image.BICUBIC, expand=True)
    cx = x0 + (bw - card.width) // 2
    cy = y0 + (bh - card.height) // 2
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    blk = Image.new("RGBA", card.size, (0, 0, 0, 255))
    blk.putalpha(card.getchannel("A").point(lambda v: int(v * 0.6)))
    shadow.alpha_composite(blk, (cx + card.width // 40, cy + card.height // 30))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(10, card.width // 30))))
    canvas.alpha_composite(card, (cx, cy))


# ------------------------------------------------------------------ main
def _line_metrics(f):
    ascent, descent = f.getmetrics()
    return ascent, descent


def _review_block(draw, quote, width, max_h, sizes, inner):
    """Pick the biggest quote size that fits; returns (font, lines, block_height)."""
    sr = 22
    chrome = inner * 2 + sr * 2 + 24 + 48  # padding + stars + attribution row
    for size in sizes:
        qf = font(BODY_FONT, size, "Medium")
        lines = _wrap(draw, f"\u201c{quote}\u201d", qf, width - inner * 2)
        h = chrome + len(lines) * int(size * 1.28)
        if h <= max_h:
            return qf, lines, h
    qf = font(BODY_FONT, sizes[-1], "Medium")
    lines = _wrap(draw, f"\u201c{quote}\u201d", qf, width - inner * 2)
    fit = max(1, int((max_h - chrome) // (sizes[-1] * 1.28)))
    if len(lines) > fit:
        lines = lines[:fit]
        lines[-1] = lines[-1].rstrip(" ,.;\u201d") + "\u2026\u201d"
    return qf, lines, chrome + len(lines) * int(sizes[-1] * 1.28)


def _draw_review_card(canvas, box, qf, lines, rating, attribution, light_bg, accent, inner):
    cx0, cy0, cx1, cy1 = box
    card = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([cx0, cy0, cx1, cy1], radius=34,
                                           fill=(12, 13, 18, 210) if not light_bg else (255, 255, 255, 230))
    canvas.alpha_composite(card)
    d = ImageDraw.Draw(canvas)
    sr = 22
    for i in range(5):
        d.polygon(_star(cx0 + inner + sr + i * sr * 2.4, cy0 + inner + sr, sr),
                  fill=(255, 196, 0) if i < rating else (90, 90, 90))
    txt = (255, 255, 255) if not light_bg else (20, 22, 28)
    y = cy0 + inner + sr * 2 + 24
    for ln in lines:
        d.text((cx0 + inner, y), ln, font=qf, fill=txt)
        y += int(qf.size * 1.28)
    af = font(BODY_FONT, 30, "SemiBold")
    attr = _wrap(d, attribution, af, (cx1 - cx0) - inner * 2)
    d.text((cx0 + inner, cy1 - inner - 30), attr[0] if attr else attribution, font=af, fill=accent)


def render(spec: dict, fmt: str) -> Image.Image:
    """spec keys: layout (product|review_photo|review_card), photo, placement (auto|cutout|framed),
    backdrop, headline, subline, review_text, rating, accent, accent2, watermark."""
    W, H = FORMATS[fmt]
    s = db.all_settings()
    accent = spec.get("accent") or s["accent_color"]
    accent2 = spec.get("accent2") or s["accent_color_2"]
    style = spec.get("backdrop") or "garage"
    seed = int(hashlib.sha1((spec.get("headline") or "x").encode()).hexdigest()[:6], 16)
    bg = backdrops.render(style, W, H, accent, accent2, seed).convert("RGBA")
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))   # text + cards layer, composited last
    draw = ImageDraw.Draw(canvas)

    layout = spec.get("layout", "product")
    landscape = W > H
    pad = int(min(W, H) * 0.06)
    gap = int(min(W, H) * 0.03)
    inner = 36
    quote = (spec.get("review_text") or "").strip().strip('"\u201c\u201d')
    has_review = layout in ("review_photo", "review_card") and bool(quote)
    has_photo = bool(spec.get("photo"))
    rating = int(spec.get("rating") or 5)
    sub = (spec.get("subline") or "").strip()
    attribution = "VERIFIED ETSY REVIEW" + (f"  \u00b7  {sub.upper()}" if sub else "")
    headline = (spec.get("headline") or "").upper().strip()

    light_bg = _luma(bg, (0, 0, W, max(10, int(H * 0.2)))) > 150
    ink = (20, 22, 28, 255) if light_bg else (255, 255, 255, 255)
    wm_space = int(H * 0.035)

    # ------------------------------------------------ text column / headline
    if landscape:
        text_x0, text_x1 = int(W * 0.58), W - pad
        col_w = text_x1 - text_x0
        y = pad
        if headline:
            f, lines = _fit_text(draw, headline, HEADLINE_FONT, col_w, 104, 44, max_lines=3)
            asc, _ = _line_metrics(f)
            for ln in lines:
                _shadowed_text(canvas, (text_x0, y + asc), ln, f, ink, anchor="ls")
                y += int(asc * 1.02)
            y += gap // 2
            draw.rectangle([text_x0, y, text_x0 + 120, y + 8], fill=accent)
            y += 8 + gap
        if has_review:
            qf, qlines, bh = _review_block(draw, quote, col_w, H - pad - wm_space - y,
                                           [52, 46, 42, 38, 34, 30, 28, 26], inner)
            _draw_review_card(canvas, (text_x0, y, text_x1, y + bh), qf, qlines, rating,
                              attribution, light_bg, accent, inner)
        elif sub:
            f = font(BODY_FONT, 40, "SemiBold")
            for ln in _wrap(draw, sub, f, col_w)[:3]:
                _shadowed_text(canvas, (text_x0, y), ln, f, ink, offset=2)
                y += int(f.size * 1.3)
        photo_box = (pad, pad, int(W * 0.54), H - pad)
    else:
        y = pad
        if headline:
            f, lines = _fit_text(draw, headline, HEADLINE_FONT, W - 2 * pad,
                                 {"square": 104}.get(fmt, 104 if has_review and has_photo else 124), 52, max_lines=2)
            asc, _ = _line_metrics(f)
            for ln in lines:
                _shadowed_text(canvas, (W // 2, y + asc), ln, f, ink, anchor="ms")
                y += int(asc * 1.02)
            y += gap // 2
            bar = int(W * 0.12)
            draw.rectangle([W // 2 - bar // 2, y, W // 2 + bar // 2, y + 8], fill=accent)
            y += 8 + gap
        head_bottom = y
        bottom = H - pad - wm_space

        if has_review:
            if layout == "review_card" and not has_photo:
                # quote is the hero: big text, card centered in the free space
                qf, qlines, bh = _review_block(draw, quote, W - 2 * pad, bottom - head_bottom - gap,
                                               [80, 72, 64, 58, 52, 46, 42, 38, 34], inner + 12)
                top = head_bottom + max(0, (bottom - head_bottom - bh) // 2)
                _draw_review_card(canvas, (pad, top, W - pad, top + bh), qf, qlines, rating,
                                  attribution, light_bg, accent, inner + 12)
                photo_box = None
            else:
                max_card = int(H * (0.30 if fmt != "square" else 0.36))
                qf, qlines, bh = _review_block(draw, quote, W - 2 * pad, max_card,
                                               [72, 64, 58, 52, 46, 42, 38, 34, 30, 28], inner)
                _draw_review_card(canvas, (pad, bottom - bh, W - pad, bottom), qf, qlines, rating,
                                  attribution, light_bg, accent, inner)
                photo_box = (pad, head_bottom, W - pad, bottom - bh - gap)
        else:
            if sub:
                f = font(BODY_FONT, 40, "SemiBold")
                lines = _wrap(draw, sub, f, W - 2 * pad - 60)[:2]
                lh = int(f.size * 1.3)
                pill_h = len(lines) * lh + 28
                pill_w = max(draw.textlength(l, font=f) for l in lines) + 64
                py0 = bottom - pill_h
                pill = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                ImageDraw.Draw(pill).rounded_rectangle(
                    [W / 2 - pill_w / 2, py0, W / 2 + pill_w / 2, bottom], radius=pill_h // 2,
                    fill=(0, 0, 0, 160) if not light_bg else (255, 255, 255, 200))
                canvas.alpha_composite(pill)
                d = ImageDraw.Draw(canvas)
                ty = py0 + 14
                for ln in lines:
                    d.text((W // 2, ty), ln, font=f, anchor="ma",
                           fill=(255, 255, 255) if not light_bg else (20, 22, 28))
                    ty += lh
                bottom = py0 - gap
            photo_box = (pad, head_bottom, W - pad, bottom)

    # ------------------------------------------------ photo (drawn under the text overlays)
    if has_photo and photo_box and photo_box[3] - photo_box[1] > 80:
        try:
            photo = load_image(spec["photo"])
            placement = spec.get("placement") or "auto"
            cut = None
            if placement == "cutout" or (placement == "auto" and layout == "product"):
                cut = cutout(photo, spec["photo"])
            if cut is not None:
                floor = style in ("garage", "studio", "trail", "sunset") and not landscape
                glow = None if light_bg else backdrops.hex_rgb(accent)
                _place_cutout(bg, cut, photo_box, floor, glow)
            else:
                _place_framed(bg, photo, photo_box, tilt=0 if layout == "product" else -2.5)
        except Exception as e:
            db.log(f"Studio: couldn't use photo {spec.get('photo')}: {e}", "warn")

    # ------------------------------------------------ watermark
    wm = (spec.get("watermark") or s.get("watermark") or "").strip()
    if wm:
        f = font(BODY_FONT, 28 if not landscape else 26, "Bold")
        _shadowed_text(canvas, (W - pad // 2, H - pad // 2), wm, f,
                       (255, 255, 255, 235) if not light_bg else (20, 22, 28, 235), offset=2, anchor="rd")
    bg.alpha_composite(canvas)
    return bg.convert("RGB")


def render_to_file(spec: dict, fmt: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    img = render(spec, fmt)
    img.save(out, "JPEG", quality=90, optimize=True, progressive=True)
    return out


def preview_bytes(spec: dict, fmt: str, max_side: int = 720) -> bytes:
    img = render(spec, fmt)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def make_video(image_path: Path, out: Path, seconds: float = 7.0) -> Path:
    """Turn a 1080x1920 graphic into a short slow-zoom MP4 (for TikTok / Reels).
    Includes a silent audio track; add trending audio in the TikTok app."""
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as e:
            raise RuntimeError("ffmpeg not found — install it (sudo apt install ffmpeg)") from e
    fps = 30
    frames = int(seconds * fps)
    vf = (f"scale=2160:-2,zoompan=z='min(zoom+0.0007,1.15)':d={frames}:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps},format=yuv420p")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-loop", "1", "-i", str(image_path),
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-vf", vf, "-t", f"{seconds}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=300)
    return out
