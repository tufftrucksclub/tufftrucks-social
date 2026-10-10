"""Demo data so you can click around before connecting any accounts (DEMO_MODE=1)."""
from __future__ import annotations

import json
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import config, db

DEMO_DIR = config.DATA_DIR / "demo"

PRODUCTS = [
    ("Flex-Fit Tow Hook Covers compatible with 2015+ Ford F-150 Raptor and Tremor", "F150", (236, 64, 122), "$35.00–$62.00"),
    ("Flex-Fit Tow Hook Covers compatible with 2019-2025 GMC Sierra", "GMC", (30, 136, 229), "$34.00"),
    ("Flex-Fit Tow Hook Covers compatible with Tesla Cybertruck", "Cybertruck", (255, 179, 0), "$39.00"),
    ("Flex-Fit Tow Hook Covers compatible with 2007-2018 RAM 1500", "Ram", (67, 160, 71), "$37.00"),
    ("Flex-Fit Tow Hook Covers compatible with Rivian R1", "Rivian", (255, 112, 67), "$39.00"),
    ("Flex-Fit Tow Hook Covers compatible with 2026+ Hyundai Palisade", "Hyundai", (126, 87, 194), "$34.00–$50.00"),
]

REVIEWS = [
    (5, "Fit perfect on my Sierra and the blue matches my paint exactly. Shipping was quick too!", 1, True),
    (5, "These are so well made. My husband loves them and gets compliments at every gas station.", 0, False),
    (5, "Exactly what I wanted, great quality. Already ordered a second set for the wife's truck.", 2, True),
    (5, "Seller was super helpful picking the right color. Snapped right in.", 3, False),
    (4, "Look great, took a minute to get them seated but they're solid now.", 4, True),
    (5, "Pink on a Raptor? Absolutely. Love these!", 0, True),
]


def _product_img(color, seed) -> Image.Image:
    rng = random.Random(seed)
    im = Image.new("RGB", (1200, 1200), (246, 246, 246))
    d = ImageDraw.Draw(im)
    dark = tuple(int(c * 0.78) for c in color)
    darker = tuple(int(c * 0.45) for c in color)
    for cx in (420, 780):
        w, h = rng.choice([(150, 230), (160, 210), (140, 240)])
        d.rounded_rectangle([cx - w, 600 - h, cx + w, 600 + h], radius=70, fill=color)
        d.rounded_rectangle([cx - w + 50, 600 - h + 60, cx + w - 50, 600 + h - 60], radius=45, fill=dark)
        d.ellipse([cx - 42, 558, cx + 42, 642], fill=darker)
    return im.filter(ImageFilter.GaussianBlur(0.8))


def _review_img(color, seed) -> Image.Image:
    rng = np.random.default_rng(seed)
    a = np.zeros((1500, 1125, 3))
    a[:] = np.linspace(120, 40, 1500)[:, None, None] * np.array([0.8, 0.85, 1.0])
    a += rng.normal(0, 7, a.shape)
    im = Image.fromarray(np.clip(a, 0, 255).astype("uint8"))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 620, 1125, 1000], fill=(28, 28, 30))
    d.rectangle([0, 600, 1125, 625], fill=(60, 60, 64))
    d.rounded_rectangle([430, 660, 700, 950], radius=55, fill=color)
    d.ellipse([525, 765, 605, 845], fill=tuple(int(c * 0.4) for c in color))
    d.rectangle([0, 1000, 1125, 1500], fill=(70, 72, 70))
    return im.filter(ImageFilter.GaussianBlur(1.1))


def seed() -> None:
    if db.q1("SELECT 1 FROM products LIMIT 1"):
        return
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    pids = []
    for i, (title, tag, color, price) in enumerate(PRODUCTS):
        path = DEMO_DIR / f"product_{i}.jpg"
        _product_img(color, i).save(path, quality=90)
        pid = f"demo:{i}"
        pids.append((pid, title, color))
        db.run("""INSERT INTO products(id, source, title, handle, url, description, product_type, tags, price,
                  images, status, created_at, updated_at) VALUES(?, 'demo', ?, ?, ?, ?, 'Tow Hook Covers', ?, ?, ?,
                  'active', ?, ?)""",
               (pid, title, f"demo-{i}", f"{config.STORE_URL}/products/demo-{i}",
                f"Flex-Fit Tow Hook Covers (2 PCS). Made to order in dozens of colors. Fits {tag} factory tow hooks.",
                json.dumps([tag, "tow hook covers"]), price, json.dumps([str(path)]), db.iso(), db.iso()))
    for j, (rating, text, pi, has_photo) in enumerate(REVIEWS):
        img = None
        if has_photo:
            path = DEMO_DIR / f"review_{j}.jpg"
            _review_img(PRODUCTS[pi][2], j).save(path, quality=88)
            img = str(path)
        db.run("""INSERT INTO reviews(id, source, listing_id, product_title, rating, text, image_url, created_at,
                  photo_score, photo_notes) VALUES(?, 'demo', ?, ?, ?, ?, ?, ?, ?, ?)""",
               (f"demo:r{j}", str(pi), PRODUCTS[pi][0], rating, text, img, db.iso(),
                7 if has_photo else None,
                json.dumps({"notes": "Demo photo", "placement": "framed", "privacy_issue": False}) if has_photo else None))
    db.log("Demo data loaded (DEMO_MODE=1)")
