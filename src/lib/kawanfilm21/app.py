#!/usr/bin/env python3
"""KawanFilm21 REST API - JSON over the scraped data.

Run:  python3 app.py --port 8080
Docs: GET /  |  GET /api  |  GET /health  |  GET /api/stats
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ads
import query as Q
import scrape
import store
import upstream as up

VERSION = "1.0.0"
STARTED = time.time()

BOOT_ERROR: str | None = None
_LIVE_KINDS: set[str] = set()

TAX_PATH = {
    "genres": "categories", "genre": "categories", "tags": "tags", "tag": "tags",
    "directors": "muvidirector", "director": "muvidirector", "cast": "muvicast",
    "years": "muviyear", "year": "muviyear", "countries": "muvicountry",
    "country": "muvicountry", "qualities": "muviquality", "quality": "muviquality",
    "indexes": "muviindex", "index": "muviindex", "networks": "muvinetwork",
    "network": "muvinetwork",
}
KIND_PATH = {"movies": "movie", "movie": "movie", "tv": "tv", "tvshows": "tv",
             "episodes": "episode", "episode": "episode", "pages": "page", "page": "page"}

ENDPOINT_INDEX = {
    "service": "KawanFilm21 REST API",
    "version": VERSION,
    "source": up.ORIGIN,
    "data_source": "WordPress REST API (/wp-json/wp/v2) + per-post detail enrichment",
    "ads": "excluded - template ad banners/floating ads/scripts and the repeated "
           "TUTORIAL+TELEGRAM promo blocks are stripped from every payload "
           "(?clean=aggressive also drops SEO keyword-spam paragraphs)",
    "endpoints": {
        "GET /health": "liveness + row counts",
        "GET /api": "this index",
        "GET /api/stats": "row counts per kind and per taxonomy",
        "GET /api/movies": "list movies (?page, ?per_page, ?search, ?genre, ?year, "
                           "?country, ?quality, ?index, ?cast, ?director, ?tag, "
                           "?orderby=date|title|id|rating|views, ?order, ?content=1)",
        "GET /api/movies/latest": "20 newest movies",
        "GET /api/movies/{id|slug}": "one movie with download links + terms",
        "GET /api/movies/{id|slug}/downloads": "just the download links",
        "GET /api/tv": "list TV shows (same filters; adds ?network=)",
        "GET /api/tv/{id|slug}": "one TV show, includes its episode list",
        "GET /api/episodes": "list episodes (?quality=)",
        "GET /api/episodes/{id|slug}": "one episode",
        "GET /api/pages": "static pages",
        "GET /api/{kind}/{id}/downloads": "flattened download links",
        "GET /api/search?q=": "search titles/synopsis/content (?kind= to narrow)",
        "GET /api/genres": "all genres (?search, ?orderby=count|name, ?order)",
        "GET /api/genres/{id|slug}": "one genre",
        "GET /api/genres/{id|slug}/items": "movies in that genre (?kind=tv)",
        "GET /api/tags|directors|cast|years|countries|qualities|indexes|networks":
            "the other taxonomies, same shape as /api/genres",
        "GET /api/{taxonomy}/{id|slug}/items": "items carrying that term",
    },
    "query_params": {
        "page": "1-based page number (default 1)",
        "per_page": "1-100 for items, up to 500 for terms (default 20 / 100)",
        "content": "content=1 (default on detail views) adds content_html + content_text",
        "clean": "ads (default) | aggressive | none",
        "fresh": "fresh=1 refetches the single item from upstream",
    },
    "notes": [
        "Items with no upstream download block return downloads: [] (not an error).",
        "'blogs' post type exists upstream but holds 0 published records.",
    ],
}


SEED_PAGES = max(1, int(os.environ.get("KF21_SEED_PAGES", "5")))


def ensure_live(con, kind: str) -> None:
    """If the DB has nothing for a kind, scrape the first pages straight from upstream.

    This is what makes the API deployable without shipping a database file: a
    fresh install has an empty DB, and the first request seeds it live. Set
    KF21_SEED_PAGES to control how many 100-item pages are pulled per kind.
    """
    if kind in _LIVE_KINDS:
        return
    have = con.execute("SELECT COUNT(*) c FROM items WHERE kind=?", (kind,)).fetchone()["c"]
    if have:
        _LIVE_KINDS.add(kind)
        return
    try:
        total = up.count(kind)
        pages = min(SEED_PAGES, max(1, (total // 100) + 1))
        print(f"seeding {kind}: {total} upstream, pulling {pages} page(s)", flush=True)
        seen = 0
        for page in range(1, pages + 1):
            batch = up.page_items(kind, page)
            if not batch:
                break
            fm = [int(b["featured_media"]) for b in batch if b.get("featured_media")]
            mm = {}
            for i in range(0, len(fm), 100):
                try:
                    mm.update(up.media_urls(fm[i:i + 100]))
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            tax_ids: dict = {}
            for raw in batch:
                row = scrape.build_row(kind, raw, mm)
                store.upsert_item(con, kind, row)
                for field in up.TERM_FIELDS.values():
                    ids = raw.get(field) or []
                    if isinstance(ids, list) and ids and isinstance(ids[0], dict):
                        ids = [t.get("id") for t in ids]
                    clean = [int(i) for i in ids if isinstance(i, int)]
                    store.set_terms(con, row["id"], kind, field, clean)
                    tax_ids.setdefault(field, set()).update(clean)
            for tax, tids in tax_ids.items():
                try:
                    scrape.ensure_terms(con, tax, sorted(tids))
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            seen += len(batch)
            print(f"  {kind}: page {page}/{pages} -> {seen}/{total}", flush=True)
            time.sleep(0.25)
        _LIVE_KINDS.add(kind)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def refresh_item(con, kind: str, ident: str) -> dict:
    """Fetch one item fresh from upstream and upsert it."""
    if str(ident).isdigit():
        raw, _ = up.api(f"{up.ENDPOINTS[kind]}/{int(ident)}")
    else:
        data, _ = up.api(up.ENDPOINTS[kind], slug=ident, per_page=1)
        if not data:
            raise Q.NotFound(f"{kind} slug '{ident}' not found upstream")
        raw = data[0]
    fm = raw.get("featured_media")
    mm = up.media_urls([fm]) if fm else {}
    row = scrape.build_row(kind, raw, mm)
    store.upsert_item(con, kind, row)
    for field in up.TERM_FIELDS.values():
        ids = raw.get(field) or []
        store.set_terms(con, row["id"], kind, field, [i for i in ids if isinstance(i, int)])
    if row.get("link"):
        try:
            html_bytes, _ = up.fetch(row["link"])
            page_html = html_bytes.decode("utf-8", "replace")
            det = up.parse_detail(page_html)
            page_dl = ads.parse_page_downloads(page_html)
            if page_dl and not row.get("downloads"):
                with store._lock:
                    con.execute("UPDATE items SET downloads=? WHERE id=? AND kind=?",
                                (json.dumps(page_dl, ensure_ascii=False), row["id"], kind))
                    con.commit()
            with store._lock:
                con.execute("UPDATE items SET rating=?, rating_count=?, duration=?, "
                            "release_date=?, language=?, views=?, enriched=1 "
                            "WHERE id=? AND kind=?",
                            (det["rating"], det.get("votes"), det["duration"],
                             det["release_date"], det["language"], det["views"],
                             row["id"], kind))
                con.commit()
        except Exception:  # noqa: BLE001
            pass
    return row


class Handler(BaseHTTPRequestHandler):
    server_version = f"KawanFilm21API/{VERSION}"
    con = None
    db_path = store.DB_PATH

    def log_message(self, fmt, *args):  # quieter, timestamped
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------------------------------------------------------------- plumbing
    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_HEAD(self):  # noqa: N802
        self._handle()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.end_headers()

    # -------------------------------------------------------------------- core
    def _handle(self):
        t0 = time.time()
        try:
            self._route()
        except Q.NotFound as e:
            self._send({"error": "not_found", "message": str(e)}, 404)
        except up.UpstreamError as e:
            self._send({"error": "upstream_unavailable", "status": e.status,
                        "message": str(e), "url": e.url}, 502)
        except ValueError as e:
            self._send({"error": "bad_request", "message": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send({"error": "internal_error", "message": str(e)}, 500)
        finally:
            self.log_message("%s %s (%.0fms)", self.command, self.path,
                             (time.time() - t0) * 1000)

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)
        parts = [p for p in path.split("/") if p]

        if path == "/":
            self._send(ENDPOINT_INDEX | {"status": "ok", "uptime_s": round(time.time() - STARTED, 1)})
            return
        if path == "/health":
            n = self.con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
            self._send({"status": "ok", "version": VERSION, "items": n,
                        "db": self.db_path, "boot_error": BOOT_ERROR,
                        "uptime_s": round(time.time() - STARTED, 1)})
            return
        if path == "/api" or path == "/api/v1":
            self._send(ENDPOINT_INDEX)
            return
        if path == "/api/stats":
            self._send({"source": up.ORIGIN, "db": self.db_path, **store.stats(self.con)})
            return
        if parts[:1] != ["api"]:
            raise Q.NotFound("unknown path")
        seg = parts[1:]
        if not seg:
            self._send(ENDPOINT_INDEX)
            return

        head = seg[0].lower()
        # /api/search
        if head == "search":
            self._send(Q.search(self.con, q))
            return
        # /api/{taxonomy}[...]
        if head in TAX_PATH:
            tax = TAX_PATH[head]
            if len(seg) == 1:
                self._send(Q.list_terms(self.con, tax, q))
                return
            if len(seg) == 3 and seg[2].lower() == "items":
                self._send(Q.term_items(self.con, tax, seg[1], q))
                return
            if len(seg) == 2:
                res = Q.term_items(self.con, tax, seg[1], q | {"per_page": ["100"]})
                res["results"] = [r for r in res.pop("results", [])]
                self._send({"source": up.ORIGIN, **res})
                return
            raise Q.NotFound("unknown taxonomy path")
        # /api/{kind}[...]
        if head in KIND_PATH:
            kind = KIND_PATH[head]
            ensure_live(self.con, kind)
            if len(seg) == 1:
                self._send(Q.list_items(self.con, kind, q))
                return
            ident = seg[1]
            if len(seg) == 2 and ident.lower() == "latest":
                lq = dict(q)
                lq.setdefault("per_page", ["20"])
                lq.setdefault("orderby", ["date"])
                lq.setdefault("order", ["desc"])
                self._send(Q.list_items(self.con, kind, lq))
                return
            if len(seg) == 3 and seg[2].lower() == "downloads":
                self._send(Q.downloads(self.con, kind, ident))
                return
            if len(seg) == 2:
                if q.get("fresh", ["0"])[0] in ("1", "true", "yes"):
                    refresh_item(self.con, kind, ident)
                self._send(Q.get_item(self.con, kind, ident, q))
                return
            raise Q.NotFound("unknown item path")
        if head == "blogs":
            self._send({"source": up.ORIGIN, "kind": "blogs", "total": 0, "results": [],
                        "note": "the upstream 'blogs' post type has 0 published records"})
            return
        raise Q.NotFound(f"unknown resource '{head}'")


def main() -> int:
    global BOOT_ERROR
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    p.add_argument("--db", default=store.DB_PATH)
    a = p.parse_args()

    con = store.connect(a.db)
    store.init(con)
    Handler.con = con
    Handler.db_path = a.db
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"KawanFilm21 REST API v{VERSION} on http://{a.host}:{a.port}  (db={a.db})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
