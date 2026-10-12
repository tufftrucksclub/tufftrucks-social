"""SQLite storage. One file (data/social.db), no server to run."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    platform TEXT PRIMARY KEY,
    data TEXT NOT NULL,              -- JSON: access_token, refresh_token, expires_at, ...
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,             -- 'shopify:<gid>' or 'etsy:<listing_id>'
    source TEXT NOT NULL,            -- shopify | etsy
    title TEXT NOT NULL,
    handle TEXT,
    url TEXT,
    description TEXT,
    product_type TEXT,
    tags TEXT,                       -- JSON list
    price TEXT,
    images TEXT,                     -- JSON list of URLs
    status TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_featured_at TEXT,
    times_featured INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,             -- 'etsy:<transaction_id>'
    source TEXT NOT NULL,
    listing_id TEXT,
    product_title TEXT,
    rating INTEGER,
    text TEXT,
    image_url TEXT,
    created_at TEXT,
    photo_score INTEGER,             -- Claude's 1-10 rating of the photo for social use
    photo_notes TEXT,
    used_at TEXT,                    -- when we last built a post from it
    hidden INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT,                      -- internal label
    angle TEXT,                      -- the idea / hook, from the planner
    product_id TEXT,
    review_id TEXT,
    source_image TEXT,               -- URL or local path of the main photo
    backdrop TEXT,                   -- backdrop style used by the studio
    overlay_text TEXT,               -- headline burned onto the graphic
    scheduled_at TEXT,               -- UTC ISO
    status TEXT NOT NULL,            -- draft | needs_approval | approved | publishing | published | partial | failed | rejected
    approval TEXT,                   -- auto | manual | null
    needs_human_reason TEXT,
    created_by TEXT,                 -- planner | manual
    week TEXT,                       -- ISO week the planner put it in, e.g. 2026-W42
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    caption TEXT,
    title TEXT,                      -- Pinterest pin title
    link TEXT,
    media_path TEXT,                 -- local rendered file for this platform
    media_url TEXT,                  -- public URL after upload
    status TEXT NOT NULL,            -- pending | published | failed | skipped | sent_to_inbox
    external_id TEXT,
    external_url TEXT,
    error TEXT,
    published_at TEXT,
    UNIQUE(post_id, platform)
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES post_targets(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    impressions INTEGER,
    reach INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    saves INTEGER,
    clicks INTEGER,
    views INTEGER,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,              -- weekly_performance | plan_notes
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,             -- info | warn | error
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,             -- 'icloud:<asset id>', 'folder:<hash>' or 'edit:<hash>'
    source TEXT NOT NULL,            -- icloud | folder | edit
    filename TEXT,
    album TEXT,
    taken_at TEXT,
    width INTEGER,
    height INTEGER,
    local_path TEXT,                 -- working JPEG, relative to the data dir (preview size for iCloud)
    original_path TEXT,              -- full-resolution file once fetched (absolute for folder photos)
    parent_id TEXT,                  -- for edits: the photo it was made from
    score INTEGER,                   -- Claude's 1-10 rating for social use
    notes TEXT,                      -- JSON: description, subject, background, placement, scene idea, ...
    rated_at TEXT,
    used_at TEXT,
    hidden INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_posts_sched ON posts(scheduled_at);
CREATE INDEX IF NOT EXISTS ix_photos_taken ON photos(taken_at);
CREATE INDEX IF NOT EXISTS ix_targets_post ON post_targets(post_id);
CREATE INDEX IF NOT EXISTS ix_metrics_target ON metrics(target_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


# Columns added after the first release; CREATE TABLE IF NOT EXISTS won't add them to an existing db.
_ADDED_COLUMNS = {
    "posts": {"photo_id": "TEXT", "placement": "TEXT"},
    "photos": {"tags": "TEXT"},     # JSON list of lowercase tags; Claude suggests them, you edit them
}


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    c.commit()


@contextmanager
def tx():
    with _lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql: str, params: tuple | dict = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


def q1(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def run(sql: str, params: tuple | dict = ()) -> int:
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


# ----------------------------------------------------------------- settings
def get_setting(key: str):
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return config.DEFAULT_SETTINGS.get(key)
    return json.loads(row["value"])


def all_settings() -> dict:
    out = dict(config.DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out


def set_setting(key: str, value) -> None:
    run("INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


# ------------------------------------------------------------------- tokens
def get_token(platform: str) -> dict | None:
    row = q1("SELECT data FROM tokens WHERE platform = ?", (platform,))
    return json.loads(row["data"]) if row else None


def save_token(platform: str, data: dict) -> None:
    run("INSERT INTO tokens(platform, data, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(platform) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (platform, json.dumps(data), iso()))


def delete_token(platform: str) -> None:
    run("DELETE FROM tokens WHERE platform = ?", (platform,))


# ----------------------------------------------------------------- activity
def log(message: str, level: str = "info") -> None:
    print(f"[{level}] {message}", flush=True)
    try:
        run("INSERT INTO activity(at, level, message) VALUES(?, ?, ?)", (iso(), level, message[:2000]))
        run("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 2000)")
    except Exception:  # never let logging break the caller
        pass


def jl(s: str | None, default=None):
    """json-load a column that may be null."""
    if not s:
        return default if default is not None else []
    try:
        return json.loads(s)
    except ValueError:
        return default if default is not None else []
