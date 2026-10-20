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


def can_cut_out() -> bool:
    try:
        import rembg  # noqa: F401
        return True
    except ImportError:
        return False


def remove_background(img: Image.Image, cache_key: str, max_side: int = 1600) -> Image.Image | None:
    """The photo with its background made transparent (same framing, not cropped), or None
    if rembg isn't installed."""
    global _rembg_session
    name = cache_key if max_side == 1600 else f"{cache_key}|{max_side}"  # 1600 keeps the existing cache names
    cache = config.CACHE_DIR / f"cut_{hashlib.sha1(name.encode()).hexdigest()[:20]}.png"
    if cache.exists():
        return Image.open(cache).convert("RGBA")
    try:
        from rembg import new_session, remove
    except ImportError:
        return None
    if _rembg_session is None:
        _rembg_session = new_session(config.env("REMBG_MODEL", "isnet-general-use"))
    src = img.convert("RGB")
    if max(src.size) > max_side:
        src.thumbnail((max_side, max_side), Image.LANCZOS)
    out = remove(src, session=_rembg_session).convert("RGBA")
    out.save(cache)
    return out


def cutout(img: Image.Image, cache_key: str) -> Image.Image | None:
    """Remove the background and crop to the subject. Returns None if rembg isn't installed or the
    result doesn't look like a clean cutout."""
    out = remove_background(img, cache_key)
    if out is None:
        return None
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


def _wrap(draw, text, f, max_w, measure=None):
    measure = measure or (lambda t: draw.textlength(t, font=f))
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if measure(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------- emoji
# The brand fonts have no emoji, so customer emojis in review quotes would draw as
# empty boxes. Draw them with the system's color emoji font instead (Segoe UI Emoji on
# Windows, Noto Color Emoji on Linux/Pi); if neither is installed, leave them out.
_EMOJI_FONTS = [Path(r"C:\Windows\Fonts\seguiemj.ttf"),
                Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
                Path("/usr/share/fonts/noto/NotoColorEmoji.ttf")]
_emoji_cache: dict = {}
_EMOJI_SKIP = {0xFE0E, 0xFE0F, 0x200D, 0x20E3}  # selectors / joiners: no glyph of their own


def _is_emoji_cp(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF
            or 0x2B00 <= cp <= 0x2BFF or 0xE0020 <= cp <= 0xE007F or cp in _EMOJI_SKIP)


def _segments(text: str) -> list[tuple[bool, str]]:
    """Split text into runs of (is_emoji, chars)."""
    out: list[list] = []
    for ch in text:
        e = _is_emoji_cp(ord(ch))
        if out and out[-1][0] == e:
            out[-1][1] += ch
        else:
            out.append([e, ch])
    return [(e, s) for e, s in out]


def _emoji_font(px: int):
    if "path" not in _emoji_cache:
        _emoji_cache["path"] = next((p for p in _EMOJI_FONTS if p.exists()), None)
    path = _emoji_cache["path"]
    if path is None:
        return None
    if px not in _emoji_cache:
        ef = None
        for size in (px, 109):  # Noto Color Emoji is a bitmap font that only loads at 109
            try:
                ef = ImageFont.truetype(str(path), size)
                break
            except OSError:
                continue
        _emoji_cache[px] = ef
    return _emoji_cache[px]


def has_emoji_font() -> bool:
    return _emoji_font(32) is not None


def strip_emoji(text: str) -> str:
    out = "".join(ch for ch in text if not _is_emoji_cp(ord(ch))
                  and not 0x1F3FB <= ord(ch) <= 0x1F3FF)
    return " ".join(out.split())


def _emoji_img(run: str, px: int):
    """Render a run of emoji as an RGBA image about px tall, or None."""
    key = ("img", run, px)
    if key in _emoji_cache:
        return _emoji_cache[key]
    ef = _emoji_font(px)
    glyphs = "".join(c for c in run if ord(c) not in _EMOJI_SKIP and not 0x1F3FB <= ord(c) <= 0x1F3FF)
    img = None
    if ef is not None and glyphs:
        try:
            w = int(math.ceil(ef.getlength(glyphs))) + 8
            h = int(ef.size * 1.4) + 8
            im = Image.new("RGBA", (max(w, 1), h), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((4, 4), glyphs, font=ef, embedded_color=True)
            bb = im.getbbox()
            if bb:
                im = im.crop((bb[0], bb[1], bb[2], bb[3]))
                scale = px / max(im.height, 1)
                img = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                                Image.LANCZOS)
        except Exception:
            img = None
    _emoji_cache[key] = img
    return img


def _rich_len(draw, text: str, f) -> float:
    total = 0.0
    for e, s in _segments(text):
        if not e:
            total += draw.textlength(s, font=f)
        else:
            im = _emoji_img(s, int(f.size * 0.86))
            if im is not None:
                total += im.width + f.size * 0.12
    return total


def _rich_text(canvas: Image.Image, xy, text: str, f, fill) -> None:
    """Draw text left-aligned at xy (top-left, like draw.text), with emoji in color."""
    d = ImageDraw.Draw(canvas)
    x, y = xy
    ascent, descent = f.getmetrics()
    for e, s in _segments(text):
        if not e:
            d.text((x, y), s, font=f, fill=fill)
            x += d.textlength(s, font=f)
            continue
        im = _emoji_img(s, int(f.size * 0.86))
        if im is None:
            continue
        x += f.size * 0.06
        top = int(y + ascent + descent * 0.2 - im.height)
        canvas.alpha_composite(im, dest=(int(x), max(0, top)))
        x += im.width + f.size * 0.06


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


def _match_light(cut: Image.Image, region: Image.Image, strength: float = 0.35) -> Image.Image:
    """Nudge the cutout's brightness toward the backdrop behind it, so a subject shot in a sunny
    parking lot doesn't glow on a night scene. Brightness only — product colors stay true."""
    a = np.asarray(cut, dtype=float)
    alpha = a[:, :, 3] > 128
    if alpha.sum() < 50:
        return cut
    luma = a[:, :, :3] @ np.array([0.299, 0.587, 0.114])
    subj = float(luma[alpha].mean())
    bg = float(np.asarray(region.convert("L"), dtype=float).mean())
    target = subj + (bg - subj) * strength
    gain = float(np.clip(target / max(subj, 1.0), 0.8, 1.15))
    a[:, :, :3] = np.clip(a[:, :, :3] * gain, 0, 255)
    return Image.fromarray(a.astype("uint8"), "RGBA")


def _place_cutout(canvas: Image.Image, cut: Image.Image, box, floor: bool, glow_color):
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    scale = min(bw / cut.width, bh / cut.height)
    cut = cut.resize((max(1, int(cut.width * scale)), max(1, int(cut.height * scale))), Image.LANCZOS)
    px = x0 + (bw - cut.width) // 2
    py = y0 + (bh - cut.height) // 2 if not floor else y1 - cut.height
    # tidy the edge: drop the faint halo of old background rembg leaves around the subject
    cut.putalpha(cut.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.7)))
    cut = _match_light(cut, canvas.crop((px, py, px + cut.width, py + cut.height)))
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


def _full_bleed(photo: Image.Image, W: int, H: int) -> Image.Image:
    """The photo fills the whole graphic, with soft shading where the text goes."""
    img = ImageOps.fit(photo.convert("RGB"), (W, H), Image.LANCZOS, centering=(0.5, 0.5)).convert("RGBA")
    shade = np.zeros((H, W), dtype=float)
    if W > H:   # landscape: text column on the right
        x = np.linspace(0, 1, W)[None, :]
        shade = np.broadcast_to(np.clip((x - 0.45) / 0.55, 0, 1) ** 1.2 * 190, (H, W)).copy()
    else:       # headline on top, cards / pill at the bottom
        y = np.linspace(0, 1, H)[:, None]
        top = np.clip(1 - y / 0.36, 0, 1) ** 1.5 * 175
        bottom = np.clip((y - 0.68) / 0.32, 0, 1) ** 1.5 * 150
        shade = np.broadcast_to(top + bottom, (H, W)).copy()
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.putalpha(Image.fromarray(shade.astype("uint8"), "L"))
    img.alpha_composite(layer)
    return img


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
        lines = _wrap(draw, f"\u201c{quote}\u201d", qf, width - inner * 2,
                      measure=lambda t, qf=qf: _rich_len(draw, t, qf))
        h = chrome + len(lines) * int(size * 1.28)
        if h <= max_h:
            return qf, lines, h
    qf = font(BODY_FONT, sizes[-1], "Medium")
    lines = _wrap(draw, f"\u201c{quote}\u201d", qf, width - inner * 2,
                  measure=lambda t: _rich_len(draw, t, qf))
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
        _rich_text(canvas, (cx0 + inner, y), ln, qf, txt)
        y += int(qf.size * 1.28)
    d = ImageDraw.Draw(canvas)
    af = font(BODY_FONT, 30, "SemiBold")
    attr = _wrap(d, attribution, af, (cx1 - cx0) - inner * 2)
    d.text((cx0 + inner, cy1 - inner - 30), attr[0] if attr else attribution, font=af, fill=accent)


def render(spec: dict, fmt: str) -> Image.Image:
    """spec keys: layout (product|review_photo|review_card), photo, placement (auto|cutout|framed|full),
    backdrop, headline, subline, review_text, rating, accent, accent2, watermark."""
    W, H = FORMATS[fmt]
    s = db.all_settings()
    accent = spec.get("accent") or s["accent_color"]
    accent2 = spec.get("accent2") or s["accent_color_2"]
    style = spec.get("backdrop") or "garage"
    seed = int(hashlib.sha1((spec.get("headline") or "x").encode()).hexdigest()[:6], 16)
    placement = spec.get("placement") or "auto"
    full = None
    if spec.get("photo") and placement == "full":
        try:
            full = _full_bleed(load_image(spec["photo"]), W, H)
        except Exception as e:
            db.log(f"Studio: couldn't use photo {spec.get('photo')}: {e}", "warn")
    bg = full if full is not None else backdrops.render(style, W, H, accent, accent2, seed).convert("RGBA")
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))   # text + cards layer, composited last
    draw = ImageDraw.Draw(canvas)

    layout = spec.get("layout", "product")
    landscape = W > H
    pad = int(min(W, H) * 0.06)
    gap = int(min(W, H) * 0.03)
    inner = 36
    quote = (spec.get("review_text") or "").strip().strip('"\u201c\u201d')
    if not has_emoji_font():
        quote = strip_emoji(quote)
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
    if has_photo and full is None and photo_box and photo_box[3] - photo_box[1] > 80:
        try:
            photo = load_image(spec["photo"])
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

