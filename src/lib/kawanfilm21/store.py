"""SQLite store for the KawanFilm21 scrape."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("KF21_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kawanfilm21.db"))

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER NOT NULL,
    kind         TEXT    NOT NULL,          -- movie | tv | episode | page
    slug         TEXT,
    title        TEXT,
    link         TEXT,
    date         TEXT,
    modified     TEXT,
    excerpt      TEXT,
    content_html TEXT,
    content_text TEXT,
    synopsis     TEXT,
    poster       TEXT,
    poster_small TEXT,
    downloads    TEXT,                      -- JSON array
    rating       REAL,
    rating_count INTEGER,
    duration     TEXT,
    release_date TEXT,
    language     TEXT,
    views        INTEGER,
    enriched     INTEGER DEFAULT 0,
    raw          TEXT,                      -- full upstream JSON (ads stripped)
    updated_at   REAL,
    PRIMARY KEY (id, kind)
);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_slug ON items(slug);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);

CREATE TABLE IF NOT EXISTS terms (
    id       INTEGER NOT NULL,
    taxonomy TEXT    NOT NULL,
    name     TEXT,
    slug     TEXT,
    link     TEXT,
    parent   INTEGER DEFAULT 0,
    count    INTEGER DEFAULT 0,
    description TEXT,
    PRIMARY KEY (id, taxonomy)
);
CREATE INDEX IF NOT EXISTS idx_terms_tax ON terms(taxonomy);

CREATE TABLE IF NOT EXISTS item_terms (
    item_id   INTEGER NOT NULL,
    item_kind TEXT    NOT NULL,
    taxonomy  TEXT    NOT NULL,
    term_id   INTEGER NOT NULL,
    PRIMARY KEY (item_id, item_kind, taxonomy, term_id)
);
CREATE INDEX IF NOT EXISTS idx_it_lookup ON item_terms(taxonomy, term_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Bring an existing database up to date without dropping scraped rows.
    have = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    if "rating_count" not in have:
        con.execute("ALTER TABLE items ADD COLUMN rating_count INTEGER")
    con.commit()


def set_meta(con, key: str, value) -> None:
    with _lock:
        con.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value) if not isinstance(value, str) else value),
        )
        con.commit()


def get_meta(con, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# Enrichment fields are owned by the enrich stage. A plain re-scrape of the
# list endpoint (which carries none of them) must not wipe them, and `enriched`
# must stay 1 once set or every core re-run re-queues 9k detail fetches.
_PRESERVE = ("rating", "rating_count", "duration", "release_date", "language", "views")


def upsert_item(con, kind: str, row: dict) -> None:
    cols = (
        "id", "kind", "slug", "title", "link", "date", "modified", "excerpt",
        "content_html", "content_text", "synopsis", "poster", "poster_small",
        "downloads", "rating", "rating_count", "duration", "release_date", "language",
        "views", "enriched", "raw", "updated_at",
    )
    data = {c: row.get(c) for c in cols}
    data["kind"] = kind
    data["updated_at"] = time.time()
    if data.get("enriched") is None:
        data["enriched"] = 0
    for j in ("downloads", "raw"):
        if data.get(j) is not None and not isinstance(data[j], str):
            data[j] = json.dumps(data[j], ensure_ascii=False)
    ph = ",".join("?" for _ in cols)
    sets = [f"{c}=excluded.{c}" for c in cols if c not in ("id", "kind")]
    for c in _PRESERVE:
        sets.append(f"{c}=COALESCE(excluded.{c}, {c})")
    sets.append("enriched=MAX(COALESCE(enriched,0), COALESCE(excluded.enriched,0))")
    upd = ",".join(sets)
    with _lock:
        con.execute(f"INSERT INTO items({','.join(cols)}) VALUES({ph}) "
                    f"ON CONFLICT(id,kind) DO UPDATE SET {upd}", [data[c] for c in cols])
        con.commit()


def set_terms(con, item_id: int, item_kind: str, taxonomy: str, term_ids: list) -> None:
    with _lock:
        con.execute("DELETE FROM item_terms WHERE item_id=? AND item_kind=? AND taxonomy=?",
                    (item_id, item_kind, taxonomy))
        con.executemany(
            "INSERT OR IGNORE INTO item_terms(item_id,item_kind,taxonomy,term_id) VALUES(?,?,?,?)",
            [(item_id, item_kind, taxonomy, int(t)) for t in term_ids or []],
        )
        con.commit()


def upsert_term(con, taxonomy: str, t: dict) -> None:
    with _lock:
        con.execute(
            "INSERT INTO terms(id,taxonomy,name,slug,link,parent,count,description) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id,taxonomy) DO UPDATE SET "
            "name=excluded.name, slug=excluded.slug, link=excluded.link, "
            "parent=excluded.parent, count=excluded.count, description=excluded.description",
            (int(t["id"]), taxonomy, t.get("name"), t.get("slug"), t.get("link"),
             int(t.get("parent") or 0), int(t.get("count") or 0), t.get("description") or ""),
        )
        con.commit()


def stats(con) -> dict:
    out = {"kinds": {}, "terms": {}}
    for r in con.execute("SELECT kind, COUNT(*) c, MAX(updated_at) u FROM items GROUP BY kind"):
        out["kinds"][r["kind"]] = {"count": r["c"], "updated_at": r["u"]}
    for r in con.execute("SELECT taxonomy, COUNT(*) c FROM terms GROUP BY taxonomy"):
        out["terms"][r["taxonomy"]] = r["c"]
    out["enriched"] = con.execute("SELECT COUNT(*) c FROM items WHERE enriched=1").fetchone()["c"]
    out["total_items"] = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    out["last_full_scrape"] = get_meta(con, "last_full_scrape")
    return out
