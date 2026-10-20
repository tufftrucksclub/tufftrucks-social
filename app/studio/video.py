"""Short vertical videos for TikTok and Instagram / Facebook Reels, made from a post's photos.

Each photo becomes a moving shot (slow zoom and pan). Photos that aren't tall enough sit in a frame over a
blurred copy of themselves, and white-background product shots are cut out onto the post's backdrop. The
headline pops in as the hook, the product name or review quote animates in, and an end card says where to
buy. Frames are drawn with Pillow and piped to ffmpeg, which also adds the music."""
from __future__ import annotations

import hashlib
import math
import random
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .. import config, db
from . import backdrops, compose

W, H = 1080, 1920
FPS = 30
K = 1.22                                  # shots are drawn this much bigger than the frame, so zooms stay sharp
PW, PH = round(W * K), round(H * K)
STYLES = {"quick": "Quick cuts — fast and punchy", "showcase": "Showcase — slow, smooth moves",
          "review": "Review — the customer's words are the star"}
MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
# TikTok and Reels draw their buttons and caption over the video: keep text below the top bar, above the
# caption area at the bottom, and clear of the like/comment buttons down the right edge.
SAFE_TOP, SAFE_BOTTOM = int(H * 0.09), int(H * 0.76)
FLOOR_STYLES = ("garage", "studio", "trail", "sunset")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # don't flash a console window on Windows


# ------------------------------------------------------------------ helpers
def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as e:
        raise RuntimeError("ffmpeg not found — install it (sudo apt install ffmpeg, "
                           "or on Windows: winget install Gyan.FFmpeg)") from e


def music_dir() -> Path:
    d = config.DATA_DIR / "music"
    d.mkdir(parents=True, exist_ok=True)
    return d


def music_tracks() -> list[str]:
    return sorted(p.name for p in music_dir().iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS)


def pick_track(choice: str | None, seed) -> Path | None:
    """choice: a file name from the music folder, 'none', or 'random' (the same track for the same seed)."""
    tracks = music_tracks()
    if choice == "none" or not tracks:
        return None
    if choice in tracks:
        return music_dir() / choice
    return music_dir() / random.Random(str(seed)).choice(tracks)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _ease_out(p: float) -> float:
    return 1 - (1 - _clamp(p)) ** 3


def _ease_in_out(p: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * _clamp(p))


def _ease_back(p: float) -> float:
    p, c = _clamp(p), 1.9
    return 1 + (c + 1) * (p - 1) ** 3 + c * (p - 1) ** 2


# ------------------------------------------------------------------ shots
def _plain_background(img: Image.Image) -> bool:
    """A product shot on a plain white/light background (most listing photos)."""
    a = np.asarray(img.convert("L").resize((64, 64)), dtype=float)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    return border.mean() > 222 and border.std() < 20


def _plate_cover(img: Image.Image) -> Image.Image:
    return ImageOps.fit(img.convert("RGB"), (PW, PH), Image.LANCZOS, centering=(0.5, 0.5))


def _plate_framed(img: Image.Image) -> Image.Image:
    """The whole photo in a frame, over a dark blurred copy of itself."""
    small = ImageOps.fit(img.convert("RGB"), (PW // 12, PH // 12), Image.BILINEAR)
    bg = small.filter(ImageFilter.GaussianBlur(3)).resize((PW, PH), Image.BICUBIC)
    bg = ImageEnhance.Brightness(bg).enhance(0.55).convert("RGBA")
    compose._place_framed(bg, img, (int(PW * 0.05), int(PH * 0.31), int(PW * 0.95), int(PH * 0.67)), tilt=0)
    return bg.convert("RGB")


def _plate_studio(img: Image.Image, src: str, style: str, accent: str, accent2: str, seed: int) -> Image.Image:
    """A white-background product shot cut out onto the backdrop (framed on it if it can't be cut out)."""
    bg = backdrops.render(style, PW, PH, accent, accent2, seed).convert("RGBA")
    box = (int(PW * 0.08), int(PH * 0.30), int(PW * 0.92), int(PH * 0.68))
    cut = compose.cutout(img, src) if compose.can_cut_out() else None
    if cut is not None:
        light = compose._luma(bg, (0, 0, PW, PH // 5)) > 150
        compose._place_cutout(bg, cut, box, style in FLOOR_STYLES, None if light else backdrops.hex_rgb(accent))
    else:
        compose._place_framed(bg, img, box, tilt=0)
    return bg.convert("RGB")


def _detail(img: Image.Image, rng: random.Random) -> Image.Image | None:
    """A tighter crop of the same photo, for an extra close-up shot. None if the photo is too small."""
    cw = min(img.width, img.height * W / H) * 0.62
    ch = cw * H / W
    if cw < 520:
        return None
    x = (img.width - cw) * _clamp(0.5 + rng.uniform(-0.35, 0.35))
    y = (img.height - ch) * _clamp(0.5 + rng.uniform(-0.25, 0.25))
    return img.crop((int(x), int(y), int(x + cw), int(y + ch)))


@dataclass
class Shot:
    """One moving shot: a plate that zooms from z0 to z1 and drifts from (fx0, fy0) to (fx1, fy1)."""
    plate: Image.Image
    z0: float
    z1: float
    fx0: float = 0.0
    fy0: float = 0.0
    fx1: float = 0.0
    fy1: float = 0.0
    punch: float = 0.0                    # quick style: starts a touch zoomed in and snaps back
    t0: float = 0.0
    t1: float = 0.0

    def frame(self, t: float) -> Image.Image:
        p = _ease_in_out((t - self.t0) / max(self.t1 - self.t0, 1e-3))
        z = (self.z0 + (self.z1 - self.z0) * p) / K
        if self.punch:
            z *= 1 + self.punch * (1 - _ease_out((t - self.t0) / 0.35))
        vw, vh = W / z, H / z            # viewport size on the plate
        fx = self.fx0 + (self.fx1 - self.fx0) * p
        fy = self.fy0 + (self.fy1 - self.fy0) * p
        x0 = (PW - vw) / 2 * (1 + fx)
        y0 = (PH - vh) / 2 * (1 + fy)
        return self.plate.transform((W, H), Image.AFFINE, (vw / W, 0, x0, 0, vh / H, y0), resample=Image.BILINEAR)


# ------------------------------------------------------------------ text sprites
@dataclass
class Sprite:
    img: Image.Image                      # RGBA
    cx: float                             # where its center sits in the frame
    cy: float
    t_in: float
    t_out: float
    anim: str = "pop"                     # pop | rise | fade
    dur: float = 0.4
    rgb: Image.Image = field(init=False, repr=False)
    alpha: Image.Image = field(init=False, repr=False)

    def __post_init__(self):
        self.rgb = self.img.convert("RGB")
        self.alpha = self.img.getchannel("A")

    def draw(self, frame: Image.Image, t: float) -> None:
        if t < self.t_in or t >= self.t_out:
            return
        p = (t - self.t_in) / self.dur
        a = _ease_out(p * (1.8 if self.anim == "pop" else 1.0)) * _clamp((self.t_out - t) / 0.25)
        if a <= 0.01:
            return
        rgb, alpha, dy = self.rgb, self.alpha, 0.0
        if self.anim == "pop" and p < 1:
            s = 0.55 + 0.45 * _ease_back(p)
            size = (max(1, round(rgb.width * s)), max(1, round(rgb.height * s)))
            rgb, alpha = rgb.resize(size, Image.BILINEAR), alpha.resize(size, Image.BILINEAR)
        elif self.anim == "rise":
            dy = 46 * (1 - _ease_out(p))
        if a < 0.99:
            alpha = alpha.point([int(v * a) for v in range(256)])
        frame.paste(rgb, (round(self.cx - rgb.width / 2), round(self.cy - rgb.height / 2 + dy)), alpha)


def _shadow(size, box, radius, blur=7, offset=(4, 7), opacity=120) -> Image.Image:
    sh = Image.new("RGBA", size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(sh).rounded_rectangle([x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]],
                                         radius, fill=(0, 0, 0, opacity))
    return sh.filter(ImageFilter.GaussianBlur(blur))


def _boxed(lines: list[str], f, fill, ink=(255, 255, 255), padx=30, pady=18, radius=20, gap=8) -> Image.Image:
    """Lines of text centered on a rounded box, with a soft shadow."""
    _, top, _, bottom = f.getbbox("H")
    lh = bottom - top
    widths = [f.getlength(ln) for ln in lines]
    w = int(max(widths)) + 2 * padx
    h = len(lines) * lh + (len(lines) - 1) * gap + 2 * pady
    m = 16
    im = _shadow((w + 2 * m, h + 2 * m), (m, m, m + w, m + h), radius)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([m, m, m + w, m + h], radius, fill=fill)
    y = m + pady - top
    for ln, lw in zip(lines, widths):
        d.text((m + (w - lw) / 2, y), ln, font=f, fill=ink)
        y += lh + gap
    return im


def _star_img(r: int, on: bool) -> Image.Image:
    m = 10
    im = Image.new("RGBA", (2 * r + 2 * m, 2 * r + 2 * m), (0, 0, 0, 0))
    pts = compose._star(r + m, r + m, r)
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x + 3, y + 5) for x, y in pts], fill=(0, 0, 0, 140))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(im).polygon(pts, fill=(255, 196, 0) if on else (110, 110, 110), outline=(40, 30, 0), width=2)
    return im


def _stars(rating: int, cy: float, t_in: float, t_out: float, r: int = 34) -> list[Sprite]:
    step = r * 2.5
    x0 = W / 2 - step * 2
    return [Sprite(_star_img(r, i < rating), x0 + i * step, cy, t_in + i * 0.09, t_out, "pop", 0.35)
            for i in range(5)]


def _hook(headline: str, t_in: float, t_out: float, accent: str, stagger: float) -> tuple[list[Sprite], int]:
    """The headline as stacked boxes that pop in one after another. Returns the sprites and their bottom edge."""
    text = compose.strip_emoji(headline).upper().strip()
    if not text:
        return [], SAFE_TOP
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f, lines = compose._fit_text(probe, text, compose.HEADLINE_FONT, int(W * 0.80) - 60, 104, 60, max_lines=3)
    sprites, y = [], SAFE_TOP + 20
    for i, ln in enumerate(lines):
        im = _boxed([ln], f, backdrops.hex_rgb(accent) + (255,))
        sprites.append(Sprite(im, W / 2, y + im.height / 2, t_in + i * stagger, t_out, "pop", 0.4))
        y += im.height - 18
    return sprites, y + 18


def _pill(text: str, cy: float, t_in: float, t_out: float) -> Sprite | None:
    text = compose.strip_emoji(text).strip()
    if not text:
        return None
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f, lines = compose._fit_text(probe, text, compose.BODY_FONT, int(W * 0.78), 46, 32, weight="SemiBold",
                                 max_lines=2)
    im = _boxed(lines, f, (10, 11, 16, 185), padx=34, pady=18, radius=34)
    return Sprite(im, W / 2, cy, t_in, t_out, "rise", 0.45)


def _quote(quote: str, top_min: int, t_in: float, accent: str) -> tuple[list[Sprite], float, int, float]:
    """The review quote on a dark card anchored just above the caption area: the card fades in, then the lines
    rise in one by one. Returns (sprites, time the last line lands, card top, reading time)."""
    quote = quote.strip().strip('"“”')
    if not compose.has_emoji_font():
        quote = compose.strip_emoji(quote)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    x0, x1, inner, attr_h = 64, W - 64, 44, 58
    max_h = SAFE_BOTTOM - top_min
    for size in (62, 56, 52, 48, 44, 40, 36, 32):
        qf = compose.font(compose.BODY_FONT, size, "Medium")
        lines = compose._wrap(probe, f"“{quote}”", qf, x1 - x0 - 2 * inner,
                              measure=lambda t, qf=qf: compose._rich_len(probe, t, qf))
        lh = int(size * 1.3)
        if 2 * inner + len(lines) * lh + attr_h <= max_h:
            break
    else:
        fit = max(1, (max_h - 2 * inner - attr_h) // lh)
        if len(lines) > fit:
            lines = lines[:fit]
            lines[-1] = lines[-1].rstrip(" ,.;”") + "…”"
    h = 2 * inner + len(lines) * lh + attr_h
    y0 = SAFE_BOTTOM - h
    card = _shadow((x1 - x0 + 40, h + 40), (20, 20, 20 + x1 - x0, 20 + h), 34, blur=10, opacity=110)
    ImageDraw.Draw(card).rounded_rectangle([20, 20, 20 + x1 - x0, 20 + h], 34, fill=(12, 13, 18, 215))
    sprites = [Sprite(card, W / 2, y0 + h / 2, t_in, 1e9, "fade", 0.35)]
    t = t_in + 0.3
    for i, ln in enumerate(lines):
        im = Image.new("RGBA", (x1 - x0 - 2 * inner, lh + 12), (0, 0, 0, 0))
        compose._rich_text(im, (0, 4), ln, qf, (255, 255, 255))
        sprites.append(Sprite(im, x0 + inner + im.width / 2, y0 + inner + i * lh + im.height / 2 - 4,
                              t + i * 0.3, 1e9, "rise", 0.4))
    t_last = t + len(lines) * 0.3
    af = compose.font(compose.BODY_FONT, 30, "SemiBold")
    attr = Image.new("RGBA", (x1 - x0 - 2 * inner, 44), (0, 0, 0, 0))
    ImageDraw.Draw(attr).text((0, 4), "VERIFIED ETSY REVIEW", font=af, fill=backdrops.hex_rgb(accent))
    sprites.append(Sprite(attr, x0 + inner + attr.width / 2, y0 + h - inner - 22, t_last, 1e9, "fade", 0.4))
    reading = min(len(quote.split()) / 3.4 + 1.0, 9.0)   # long quotes: people pause the video to finish
    return sprites, t_last, y0, reading


def _end_card(t0: float, t1: float, s: dict, quick: bool) -> list[Sprite]:
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    out = []
    brand = (s.get("brand_name") or "").upper().strip()
    cta = compose.strip_emoji(s.get("video_cta") or "").upper().strip()
    domain = (s.get("watermark") or "").strip() or urlparse(config.STORE_URL).netloc
    d0 = t0 + (0.05 if quick else 0.25)
    y = H * 0.36
    if brand:
        f = compose.font(compose.BODY_FONT, 44, "SemiBold")
        im = Image.new("RGBA", (int(f.getlength(brand)) + 40, 80), (0, 0, 0, 0))
        compose._shadowed_text(im, (20, 14), brand, f, (255, 255, 255, 230), offset=3)
        out.append(Sprite(im, W / 2, y, d0, t1 + 1, "rise", 0.45))
        y += 90
    if cta:
        f, lines = compose._fit_text(probe, cta, compose.HEADLINE_FONT, int(W * 0.84), 170, 80, max_lines=2)
        asc, _ = f.getmetrics()
        im = Image.new("RGBA", (W, int(len(lines) * asc * 1.02) + 50), (0, 0, 0, 0))
        ly = 20 + asc
        for ln in lines:
            compose._shadowed_text(im, (W // 2, ly), ln, f, (255, 255, 255, 255), offset=5, anchor="ms")
            ly += int(asc * 1.02)
        out.append(Sprite(im, W / 2, y + im.height / 2, d0 + 0.15, t1 + 1, "pop", 0.45))
        y += im.height + 30
    if domain:
        f = compose.font(compose.BODY_FONT, 52, "Bold")
        im = _boxed([domain.upper()], f, backdrops.hex_rgb(s["accent_color"]) + (255,), padx=40, pady=24, radius=40)
        out.append(Sprite(im, W / 2, y + im.height / 2, d0 + 0.4, t1 + 1, "rise", 0.45))
    return out


# ------------------------------------------------------------------ main
def render(spec: dict, out: Path) -> Path:
    """spec keys: photos (list of image sources, best first), style (quick|showcase|review), headline, subline,
    review_text, rating, backdrop, music (Path or None), seed."""
    started = time.monotonic()
    s = db.all_settings()
    accent, accent2 = s["accent_color"], s["accent_color_2"]
    style = spec.get("style") if spec.get("style") in STYLES else "quick"
    quick = style == "quick"
    backdrop = spec.get("backdrop") or "garage"
    seed = int(hashlib.sha1(str(spec.get("seed", "")).encode()).hexdigest()[:6], 16)
    rng = random.Random(seed)

    # ---- plates: one per photo, plus close-ups when there are only a few photos
    plates: list[tuple[Image.Image, bool]] = []          # (plate, can pan)
    details = []
    for src in spec.get("photos") or []:
        try:
            img = compose.load_image(src).convert("RGB")
        except Exception as e:
            db.log(f"Video: couldn't load photo {src}: {e}", "warn")
            continue
        if _plain_background(img):
            plates.append((_plate_studio(img, src, backdrop, accent, accent2, seed), False))
        elif img.width / img.height <= 0.8:
            plates.append((_plate_cover(img), True))
            details.append(img)
        else:
            plates.append((_plate_framed(img), False))
            details.append(img)
    want = {"quick": 5, "showcase": 3, "review": 2}[style]
    for img in details:
        if len(plates) >= want:
            break
        crop = _detail(img, rng)
        if crop is not None:
            plates.insert(min(len(plates), 1 + len(plates) // 2), (_plate_cover(crop), True))
    if not plates:
        plates.append((backdrops.render(backdrop, PW, PH, accent, accent2, seed).convert("RGB"), False))
    if quick:   # quick cuts need a few shots to cut between: reuse photos with different moves
        base = list(plates)
        while len(plates) < 4:
            plates.append(base[len(plates) % len(base)])
    plates = plates[:7]

    # ---- timing
    n = len(plates)
    xfade = 0.0 if quick else 0.5
    if quick:
        durs = [1.7] + [1.15] * (n - 1)
        durs = [d * max(1.0, 6.0 / sum(durs)) for d in durs]
    else:
        durs = [2.8 if style == "showcase" else 3.0] * n

    sprites: list[Sprite] = []
    hook_in = 0.05 if quick else 0.3
    hook, hook_bottom = _hook(spec.get("headline") or "", hook_in, 1e9, accent, 0.12 if quick else 0.18)
    review_text = (spec.get("review_text") or "").strip()
    rating = int(spec.get("rating") or 5)
    if style == "review" and review_text:
        t_card = min(durs[0] * 0.75, 2.0)
        qs, t_last, card_top, reading = _quote(review_text, hook_bottom + 110, t_card, accent)
        sprites += _stars(rating, card_top - 60, t_card - 0.5, 1e9)
        sprites += qs
        need = t_last + reading
        if sum(durs) - xfade * (n - 1) < need:
            durs = [d * need / (sum(durs) - xfade * (n - 1)) for d in durs]
    else:
        pill = _pill(spec.get("subline") or "", H * 0.70, durs[0] if quick else 1.3, 1e9)
        if pill:
            sprites.append(pill)
        if review_text:
            sprites += _stars(rating, H * 0.70 - (pill.img.height / 2 + 50 if pill else 0), 0.8, 1e9)
    sprites = hook + sprites

    # shots: each one overlaps the next by xfade (a crossfade); quick style hard-cuts with a punch-in
    shots, t = [], 0.0
    zoom_in = rng.random() < 0.5
    for i, ((plate, pans), d) in enumerate(zip(plates, durs)):
        z_far, z_near = (1.0, 1.15) if quick else (1.0, 1.10) if style == "showcase" else (1.0, 1.07)
        z0, z1 = (z_far, z_near) if zoom_in else (z_near, z_far)
        zoom_in = not zoom_in
        fx0, fx1, fy0, fy1 = ((rng.uniform(-0.7, 0.7), rng.uniform(-0.7, 0.7), rng.uniform(-0.4, 0.4),
                               rng.uniform(-0.4, 0.4)) if pans else (0, 0, 0, 0))
        shots.append(Shot(plate, z0, z1, fx0, fy0, fx1, fy1, punch=0.07 if quick and i else 0.0,
                          t0=t, t1=t + d + xfade))
        t += d
    end_start = t
    end_len = 2.0 if quick else 2.6
    total = end_start + end_len
    for sp in sprites:                                    # photo-section text leaves as the end card arrives
        sp.t_out = min(sp.t_out, end_start + (0.15 if quick else xfade * 0.6))
    end_plate = backdrops.render(backdrop, PW, PH, accent, accent2, seed + 7).convert("RGB")
    shots.append(Shot(end_plate, 1.0, 1.06, t0=end_start, t1=total + 0.1))
    sprites += _end_card(end_start, total, s, quick)

    def frame(t: float) -> Image.Image:
        live = [sh for sh in shots if sh.t0 <= t < sh.t1]
        img = live[0].frame(t)
        if len(live) > 1:
            nxt = live[1]
            img = Image.blend(img, nxt.frame(t), _ease_in_out((t - nxt.t0) / max(live[0].t1 - nxt.t0, 1e-3)))
        elif quick and live[0].t0 > 0 and t - live[0].t0 < 0.1:     # a quick white flash on each cut
            img = Image.blend(img, Image.new("RGB", (W, H), (255, 255, 255)), 0.28 * (1 - (t - live[0].t0) / 0.1))
        for sp in sprites:
            sp.draw(img, t)
        return img

    # ---- encode
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    track = spec.get("music")
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    if track:
        cmd += ["-stream_loop", "-1", "-i", str(track),
                "-af", f"afade=t=in:d=0.3,afade=t=out:st={max(total - 1.6, 0):.2f}:d=1.6"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += ["-map", "0:v", "-map", "1:a", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(tmp)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    try:
        for i in range(int(round(total * FPS))):
            proc.stdin.write(frame(i / FPS).tobytes())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait(timeout=300) != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg couldn't make the video: {err.strip()[-400:]}")
    tmp.replace(out)
    db.log(f"Rendered a {total:.0f}s {style} video from {len(spec.get('photos') or [])} photo(s)"
           f"{' with ' + Path(track).name if track else ''} in {time.monotonic() - started:.0f}s")
    return out


def still_video(image_path: Path, out: Path, seconds: float = 7.0) -> Path:
    """Fallback: a slow zoom on one finished 1080x1920 graphic, with a silent audio track."""
    frames = int(seconds * FPS)
    vf = (f"scale=2160:-2,zoompan=z='min(zoom+0.0007,1.15)':d={frames}:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},format=yuv420p")
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-loop", "1", "-i", str(image_path),
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-vf", vf, "-t", f"{seconds}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=300, creationflags=_NO_WINDOW)
    return out
