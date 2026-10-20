"""Give a photo a new background, saved as a new photo in your library.

Two ways:
  * Backdrop (free, offline): cut the subject out and set it on any Studio backdrop, including your own
    backdrop photos in data/backdrops/.
  * AI scene (needs OPENAI_API_KEY): the subject is masked off and an image model paints a photorealistic
    scene around it, matched to the photo's light and camera angle. The original subject pixels are then
    laid back on top, so the truck and our products are never altered — only the surroundings change.
"""
from __future__ import annotations

import base64
import io

import httpx
import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .. import config, db
from ..sources import photos
from . import backdrops, compose

SCENES = {
    "mountain": "a rocky mountain overlook at golden hour, dramatic clouds, warm low sun",
    "desert": "a red-rock desert dirt road at sunset, long shadows, warm light",
    "forest": "a muddy trail through tall pine forest, morning mist with sun rays",
    "city": "a wet downtown street at night with neon reflections on the pavement",
    "beach": "a wide sandy beach at sunrise with soft pastel sky and gentle surf",
    "snow": "a snowy mountain pass under a clear deep-blue sky, fresh powder",
    "saltflats": "endless white salt flats at dusk under a pink and purple sky",
    "showroom": "a dark automotive photo studio with dramatic rim lighting and a glossy black floor",
}


def ai_available() -> bool:
    return bool(config.env("OPENAI_API_KEY")) and compose.can_cut_out()


def options() -> dict[str, str]:
    """Every background choice for the UI: AI scenes (if set up) then Studio backdrops."""
    out = {}
    if ai_available():
        out.update({f"scene:{k}": "AI scene: " + v.split(",")[0] for k, v in SCENES.items()})
    if compose.can_cut_out():
        out.update({k: "Backdrop: " + v for k, v in backdrops.list_styles().items() if not k.startswith("ai:")})
    return out


def _canvas_size(w: int, h: int) -> tuple[int, int]:
    r = w / h
    return (1024, 1536) if r < 0.85 else ((1536, 1024) if r > 1.18 else (1024, 1024))


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _subject_alpha(src: Image.Image, key: str) -> Image.Image:
    cut = compose.remove_background(src, key, max_side=2048)
    if cut is None:
        raise RuntimeError("Background removal isn't installed (pip install -r requirements-cutout.txt)")
    alpha = cut.getchannel("A").resize(src.size, Image.LANCZOS)
    coverage = float((np.asarray(alpha) > 128).mean())
    if coverage < 0.02:
        raise RuntimeError("Couldn't find a clear subject in this photo to keep")
    return alpha


def _clean_edge(alpha: Image.Image) -> Image.Image:
    # pull the edge in a hair so none of the old background rims the subject
    return alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1))


def _with_backdrop(src: Image.Image, alpha: Image.Image, style: str) -> Image.Image:
    W, H = _canvas_size(*src.size)
    W, H = W * 3 // 2, H * 3 // 2
    s = db.all_settings()
    bg = backdrops.render(style, W, H, s["accent_color"], s["accent_color_2"], seed=len(style)).convert("RGBA")
    cut = src.convert("RGBA")
    cut.putalpha(alpha)
    bbox = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    cut = cut.crop(bbox) if bbox else cut
    pad = int(min(W, H) * 0.06)
    compose._place_cutout(bg, cut, (pad, int(H * 0.16), W - pad, H - pad), floor=True, glow_color=None)
    return bg.convert("RGB")


def _with_ai_scene(src: Image.Image, alpha: Image.Image, scene: str, subject: str) -> Image.Image:
    api_key = config.env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("AI scenes need OPENAI_API_KEY in .env")
    cw, ch = _canvas_size(*src.size)
    scale = min(cw / src.width, ch / src.height)
    pw, ph = max(1, round(src.width * scale)), max(1, round(src.height * scale))
    ox, oy = (cw - pw) // 2, (ch - ph) // 2

    # What the model sees: the photo, letterboxed onto the canvas over a blurred stretch of itself.
    canvas = ImageOps.fit(src, (cw, ch), Image.LANCZOS).filter(ImageFilter.GaussianBlur(24))
    canvas.paste(src.resize((pw, ph), Image.LANCZOS), (ox, oy))
    # The mask: opaque = keep (the subject), transparent = paint a new scene.
    keep = Image.new("L", (cw, ch), 0)
    keep.paste(alpha.resize((pw, ph), Image.LANCZOS).point(lambda v: 255 if v > 128 else 0), (ox, oy))
    mask = Image.new("RGBA", (cw, ch), (255, 255, 255, 0))
    mask.putalpha(keep)

    prompt = (f"Replace the entire background of this photo with: {scene}. Keep the {subject} exactly as it is — "
              "same shape, colors, details, position and size. Make the new surroundings photorealistic and match "
              "the original photo's lighting direction, color temperature, perspective and camera height, with "
              "natural ground contact shadows. No other vehicles, no people, no text, no logos, no watermarks.")
    model = config.env("OPENAI_IMAGE_MODEL", "gpt-image-1")
    data = {"model": model, "prompt": prompt, "size": f"{cw}x{ch}", "n": "1",
            "quality": config.env("OPENAI_IMAGE_QUALITY", "high")}
    if model == "gpt-image-1":
        data["input_fidelity"] = "high"
    files = {"image": ("photo.png", _png(canvas), "image/png"), "mask": ("mask.png", _png(mask), "image/png")}
    r = httpx.post("https://api.openai.com/v1/images/edits", headers={"Authorization": f"Bearer {api_key}"},
                   data=data, files=files, timeout=300)
    if r.status_code == 400 and "input_fidelity" in r.text:
        data.pop("input_fidelity", None)
        r = httpx.post("https://api.openai.com/v1/images/edits", headers={"Authorization": f"Bearer {api_key}"},
                       data=data, files=files, timeout=300)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI said {r.status_code}: {r.text[:300]}")
    item = r.json()["data"][0]
    raw = base64.b64decode(item["b64_json"]) if item.get("b64_json") else httpx.get(item["url"], timeout=60).content
    scene_img = Image.open(io.BytesIO(raw)).convert("RGB")

    # Lay the untouched original back on top, at up to 2x the canvas so the subject stays sharp.
    k = max(1.0, min(2.0, 1 / scale))
    fw, fh = round(cw * k), round(ch * k)
    out = scene_img.resize((fw, fh), Image.LANCZOS)
    sub = src.resize((round(pw * k), round(ph * k)), Image.LANCZOS)
    out.paste(sub, (round(ox * k), round(oy * k)), _clean_edge(alpha.resize(sub.size, Image.LANCZOS)))
    return out


def replace_background(photo_id: str, choice: str) -> str:
    """choice: 'scene:<key>', 'scene:<your own words>', or a Studio backdrop style. Returns the new photo's id."""
    row = photos.get(photo_id)
    if not row:
        raise KeyError(photo_id)
    path = photos.best_image(photo_id)
    src = compose.load_image(path).convert("RGB")
    alpha = _subject_alpha(src, path)
    if choice.startswith("scene:"):
        scene = choice[6:].strip()
        scene = SCENES.get(scene, scene)
        if not scene:
            raise ValueError("Describe the scene you want")
        subject = {"product_closeup": "product", "making_or_shop": "main subject"}.get(row["n"].get("subject"), "vehicle")
        img = _with_ai_scene(src, alpha, scene, subject)
        label = scene.split(",")[0]
    else:
        img = _with_backdrop(src, alpha, choice)
        label = backdrops.list_styles().get(choice, choice)
    new_id = photos.add_edit(row, img, label)
    db.log(f"Photos: new background for {row['filename']} → {label}")
    return new_id
