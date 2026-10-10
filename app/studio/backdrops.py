"""Backdrops for post graphics.

Built-in styles are drawn in code (free, instant, on-brand, work offline on a Pi).
Drop your own photos (your garage, a trail, a truck meet) into data/backdrops/ and
they show up as "photo:<filename>" styles. If OPENAI_API_KEY is set, "ai:<scene>"
styles generate a scene once and cache it.
"""
from __future__ import annotations

import base64
import hashlib
import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .. import config

CUSTOM_DIR = config.DATA_DIR / "backdrops"
CUSTOM_DIR.mkdir(parents=True, exist_ok=True)

BUILTIN = {
    "garage": "Dark shop floor with a spotlight",
    "racing": "Racing stripes on dark gradient",
    "sunset": "Sunset sky over the horizon",
    "neon": "Synthwave neon grid",
    "trail": "Mountain trail at dusk",
    "studio": "Clean light studio sweep",
    "brand": "Brand color with halftone dots",
}

AI_SCENES = {
    "ai:muddy-trail": "a muddy off-road trail through a pine forest at golden hour, dramatic light",
    "ai:night-city": "a wet city street at night with neon reflections, cinematic",
    "ai:desert": "a desert dirt road under a huge orange sunset sky",
    "ai:shop": "a clean custom truck garage with diamond-plate walls and work lights",
}


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def list_styles() -> dict[str, str]:
    out = dict(BUILTIN)
    for p in sorted(CUSTOM_DIR.glob("*")):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            out[f"photo:{p.name}"] = f"Your photo: {p.stem}"
    if config.env("OPENAI_API_KEY"):
        for k, v in AI_SCENES.items():
            out[k] = "AI scene: " + v.split(",")[0]
    return out


# --------------------------------------------------------------- helpers
def _vgrad(w: int, h: int, top, bottom) -> np.ndarray:
    t = np.linspace(0, 1, h)[:, None, None]
    a = np.array(top, dtype=float)[None, None, :]
    b = np.array(bottom, dtype=float)[None, None, :]
    return np.broadcast_to(a * (1 - t) + b * t, (h, w, 3)).copy()


def _radial(w: int, h: int, cx: float, cy: float, radius: float) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / radius
    return np.clip(1 - d, 0, 1)


def _noise(w: int, h: int, amount: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = rng.normal(0, 1, (h // 2 + 1, w // 2 + 1))
    img = Image.fromarray(((n - n.min()) / (n.max() - n.min()) * 255).astype("uint8"))
    img = img.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.2))
    return (np.asarray(img, dtype=float) / 255 - 0.5)[:, :, None] * amount


def _to_img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "RGB")


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.fit(img.convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.55))


# --------------------------------------------------------------- styles
def garage(w, h, accent, accent2, seed):
    a = _vgrad(w, h, (34, 36, 40), (12, 13, 15))
    floor_y = int(h * 0.68)
    floor = _vgrad(w, h - floor_y, (58, 60, 64), (20, 21, 24))
    a[floor_y:] = floor
    a += _noise(w, h, 22, seed)
    spot = _radial(w, h, w / 2, h * 0.62, max(w, h) * 0.55)[:, :, None]
    a = a * (0.55 + 0.75 * spot)
    a += np.array(hex_rgb(accent))[None, None, :] * 0.10 * spot
    img = _to_img(a)
    d = ImageDraw.Draw(img)
    d.line([(0, floor_y), (w, floor_y)], fill=(80, 82, 88), width=max(2, w // 400))
    # pegboard dots on the wall
    step = max(18, w // 40)
    for y in range(step, floor_y - step, step):
        for x in range(step // 2, w, step):
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(26, 27, 30))
    return img


def racing(w, h, accent, accent2, seed):
    a = _vgrad(w, h, (20, 22, 30), (6, 6, 9))
    a += _noise(w, h, 10, seed)
    img = _to_img(a).convert("RGBA")
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c1, c2 = hex_rgb(accent), hex_rgb(accent2)
    band = w * 0.16
    off = w * 0.18
    for i, (col, width) in enumerate([(c1, band), ((255, 255, 255), band * 0.18), (c2, band * 0.55)]):
        x0 = off + sum([band, band * 0.18 + band * 0.12][:i]) + i * band * 0.12
        d.polygon([(x0, h), (x0 + width, h), (x0 + width + h * 0.55, 0), (x0 + h * 0.55, 0)],
                  fill=col + (235,))
    layer = layer.filter(ImageFilter.GaussianBlur(0.6))
    img.alpha_composite(layer)
    vign = _radial(w, h, w / 2, h / 2, max(w, h) * 0.8)
    out = np.asarray(img.convert("RGB"), dtype=float) * (0.45 + 0.55 * vign[:, :, None])
    return _to_img(out)


def sunset(w, h, accent, accent2, seed):
    horizon = int(h * 0.66)
    mid = int(horizon * 0.55)
    sky = np.zeros((horizon, w, 3))
    sky[:mid] = _vgrad(w, mid, (40, 30, 90), (190, 70, 110))
    sky[mid:] = _vgrad(w, horizon - mid, (190, 70, 110), (255, 170, 80))
    a = np.zeros((h, w, 3))
    a[:horizon] = sky
    a[horizon:] = _vgrad(w, h - horizon, (60, 30, 40), (15, 10, 14))
    sun = _radial(w, h, w * 0.5, horizon - h * 0.02, w * 0.2)[:, :, None]
    a = a + np.array([255, 200, 120])[None, None, :] * np.clip(sun * 1.6, 0, 1) * 0.9
    img = _to_img(a)
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for layer, (col, amp, base) in enumerate([((70, 35, 70), 0.10, 0.02), ((40, 20, 40), 0.06, 0.0)]):
        pts = [(0, horizon)]
        phase = rnd.random() * 6
        for x in range(0, w + 20, 20):
            y = horizon - h * base - h * amp * (0.5 + 0.5 * math.sin(x / w * 7 + phase)) * (0.6 + 0.4 * math.sin(x / w * 17))
            pts.append((x, y))
        pts += [(w, horizon), (w, h), (0, h)]
        d.polygon(pts, fill=col)
    # road
    d.polygon([(w * 0.47, horizon), (w * 0.53, horizon), (w * 0.95, h), (w * 0.05, h)], fill=(28, 24, 30))
    for i in range(6):
        t0, t1 = (i + 0.2) / 6, (i + 0.55) / 6
        y0, y1 = horizon + (h - horizon) * t0 ** 1.6, horizon + (h - horizon) * t1 ** 1.6
        hw0, hw1 = w * 0.004 + w * 0.012 * t0 ** 1.6, w * 0.004 + w * 0.012 * t1 ** 1.6
        d.polygon([(w / 2 - hw0, y0), (w / 2 + hw0, y0), (w / 2 + hw1, y1), (w / 2 - hw1, y1)], fill=(240, 200, 90))
    return img


def neon(w, h, accent, accent2, seed):
    horizon = int(h * 0.58)
    a = _vgrad(w, h, (12, 6, 30), (4, 2, 10))
    glow = np.exp(-((np.arange(h) - horizon) / (h * 0.06)) ** 2)[:, None, None]
    a += np.array(hex_rgb(accent2))[None, None, :] * glow * 0.8
    img = _to_img(a + _noise(w, h, 8, seed))
    d = ImageDraw.Draw(img)
    c = hex_rgb(accent)
    for i in range(1, 16):  # horizontal lines, perspective spaced
        t = (i / 15) ** 2.2
        y = horizon + (h - horizon) * t
        d.line([(0, y), (w, y)], fill=c, width=max(1, int(1 + 3 * t)))
    for i in range(-14, 15):  # converging lines
        d.line([(w / 2 + i * w * 0.02, horizon), (w / 2 + i * w * 0.16, h)], fill=c, width=2)
    sun_r = w * 0.17
    sun = Image.new("L", (w, h), 0)
    ImageDraw.Draw(sun).ellipse([w / 2 - sun_r, horizon - sun_r * 1.6, w / 2 + sun_r, horizon + sun_r * 0.4], fill=255)
    stripes = ImageDraw.Draw(sun)
    for k in range(6):
        y = horizon - sun_r * 0.5 + k * sun_r * 0.16
        stripes.rectangle([0, y, w, y + 3 + k * 2], fill=0)
    stripes.rectangle([0, horizon, w, h], fill=0)
    sun_col = Image.new("RGB", (w, h), hex_rgb(accent2))
    img.paste(sun_col, (0, 0), sun.filter(ImageFilter.GaussianBlur(1)))
    return img.filter(ImageFilter.GaussianBlur(0.4))


def trail(w, h, accent, accent2, seed):
    a = _vgrad(w, h, (70, 90, 120), (230, 160, 100))
    img = _to_img(a + _noise(w, h, 6, seed))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    layers = [((95, 90, 110), 0.45, 0.20), ((65, 60, 72), 0.55, 0.16), ((40, 36, 40), 0.66, 0.10)]
    for col, base, amp in layers:
        pts = [(0, h)]
        x = 0
        y = h * base
        while x <= w + 40:
            y = min(h * (base + 0.08), max(h * (base - amp), y + rnd.uniform(-h * 0.035, h * 0.035)))
            pts.append((x, y))
            x += w / 24
        pts += [(w, h)]
        d.polygon(pts, fill=col)
    ground_y = int(h * 0.72)
    dirt = _vgrad(w, h - ground_y, (92, 66, 44), (40, 28, 20)) + _noise(w, h - ground_y, 40, seed + 1)
    img.paste(_to_img(dirt), (0, ground_y))
    return img


def studio(w, h, accent, accent2, seed):
    a = _vgrad(w, h, (246, 247, 250), (205, 210, 218))
    spot = _radial(w, h, w / 2, h * 0.45, max(w, h) * 0.7)[:, :, None]
    a = a * (0.88 + 0.14 * spot)
    a += np.array(hex_rgb(accent))[None, None, :] * 0.05
    return _to_img(a)


def brand(w, h, accent, accent2, seed):
    c = np.array(hex_rgb(accent), dtype=float)
    a = _vgrad(w, h, tuple(c * 1.0), tuple(c * 0.45))
    img = _to_img(a)
    d = ImageDraw.Draw(img)
    step = max(16, w // 45)
    for y in range(0, h, step):
        for x in range(0, w, step):
            r = step * 0.42 * (y / h) ** 1.3
            if r > 0.6:
                d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(v * 0.35) for v in c))
    return img


_FUNCS = {"garage": garage, "racing": racing, "sunset": sunset, "neon": neon,
          "trail": trail, "studio": studio, "brand": brand}


def _ai_backdrop(key: str, w: int, h: int) -> Image.Image | None:
    """Generate (once) and cache an AI scene. Optional; needs OPENAI_API_KEY."""
    api_key = config.env("OPENAI_API_KEY")
    if not api_key:
        return None
    prompt = AI_SCENES.get(key, key[3:])
    full = (f"Photographic background plate: {prompt}. Empty scene with clear open space in the "
            "center foreground for a product to be placed. No vehicles, no people, no text, no logos.")
    size = "1024x1536" if h > w * 1.1 else ("1536x1024" if w > h * 1.1 else "1024x1024")
    cache = config.CACHE_DIR / f"ai_{hashlib.sha1((full + size).encode()).hexdigest()[:16]}.png"
    if not cache.exists():
        import httpx
        r = httpx.post("https://api.openai.com/v1/images/generations",
                       headers={"Authorization": f"Bearer {api_key}"},
                       json={"model": config.env("OPENAI_IMAGE_MODEL", "gpt-image-1"),
                             "prompt": full, "size": size, "n": 1}, timeout=180)
        r.raise_for_status()
        item = r.json()["data"][0]
        if item.get("b64_json"):
            cache.write_bytes(base64.b64decode(item["b64_json"]))
        else:
            cache.write_bytes(httpx.get(item["url"], timeout=60).content)
    return _cover(Image.open(cache), w, h)


def render(style: str, w: int, h: int, accent: str = "#1E88E5", accent2: str = "#FF3D7F",
           seed: int = 7) -> Image.Image:
    if style.startswith("photo:"):
        p = CUSTOM_DIR / style.split(":", 1)[1]
        if p.exists():
            img = _cover(Image.open(p), w, h)
            # darken a touch so the product and text pop
            return Image.blend(img, Image.new("RGB", (w, h), (0, 0, 0)), 0.18)
        style = "garage"
    if style.startswith("ai:"):
        try:
            img = _ai_backdrop(style, w, h)
            if img is not None:
                return img
        except Exception as e:  # fall back quietly to a built-in style
            from .. import db
            db.log(f"AI backdrop failed ({e}); using garage", "warn")
        style = "garage"
    fn = _FUNCS.get(style, garage)
    return fn(w, h, accent, accent2, seed)
