#!/usr/bin/env python3
"""Bulk scraper: web.kawanfilm21.co -> SQLite.

Usage:
  python3 scrape.py --stage core            # REST API: all items + terms + posters
  python3 scrape.py --stage enrich          # per-post detail page: rating/duration/views
  python3 scrape.py --stage all             # both (default)
  python3 scrape.py --stage core --kinds movie,tv
  python3 scrape.py --stats
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
import traceback

import ads
import store
import upstream as up


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_row(kind: str, raw: dict, media_map: dict) -> dict:
    chtml = ((raw.get("content") or {}).get("rendered") or "")
    clean_html = ads.strip_ads(chtml)
    poster, poster_small = None, None
    fm = raw.get("featured_media")
    if fm and int(fm) in media_map:
        poster = media_map[int(fm)]["full"]
        poster_small = media_map[int(fm)]["thumb"]
    return {
        "id": int(raw["id"]),
        "slug": raw.get("slug"),
        "title": ((raw.get("title") or {}).get("rendered") or "").strip(),
        "link": raw.get("link"),
        "date": raw.get("date"),
        "modified": raw.get("modified"),
        "excerpt": ads.text_of((raw.get("excerpt") or {}).get("rendered") or ""),
        "content_html": clean_html,
        "content_text": ads.text_of(clean_html),
        "synopsis": ads.parse_synopsis(chtml),
        "poster": poster,
        "poster_small": poster_small,
        "downloads": ads.parse_downloads(chtml),
    }


def ensure_terms(con, taxonomy: str, ids: list) -> None:
    """Backfill any referenced term ids that are not in the terms table.

    item_terms rows are useless without the matching terms row: terms_for()
    INNER JOINs them, so an un-backfilled id silently disappears from output.
    """
    ids = [int(i) for i in ids if i]
    if not ids:
        return
    ph = ",".join("?" * len(ids))
    have = {r["id"] for r in con.execute(
        f"SELECT id FROM terms WHERE taxonomy=? AND id IN ({ph})", [taxonomy] + ids)}
    missing = [i for i in ids if i not in have]
    for i in range(0, len(missing), 100):
        for t in up.terms_by_include(taxonomy, missing[i:i + 100]):
            store.upsert_term(con, taxonomy, t)


def scrape_core(con, kinds: list[str]) -> dict:
    counts = {}
    for kind in kinds:
        rest_base = up.ENDPOINTS[kind]
        total = up.count(kind)
        log(f"{kind}: {total} records upstream")
        counts[kind] = total
        page, seen = 1, 0
        while seen < total and page <= (total // 100) + 3:
            try:
                batch = up.page_items(kind, page)
            except up.UpstreamError as e:
                log(f"  {kind} page {page} failed ({e}); retrying once")
                time.sleep(3)
                batch = up.page_items(kind, page)
            if not batch:
                break
            fm_ids = [int(b["featured_media"]) for b in batch if b.get("featured_media")]
            media_map = {}
            for i in range(0, len(fm_ids), 100):
                try:
                    media_map.update(up.media_urls(fm_ids[i:i + 100]))
                except Exception as e:  # noqa: BLE001
                    log(f"  media batch failed: {e}")
            tax_ids: dict = {}
            for raw in batch:
                row = build_row(kind, raw, media_map)
                store.upsert_item(con, kind, row)
                for rest_base_tax, field in up.TERM_FIELDS.items():
                    ids = raw.get(field) or []
                    if isinstance(ids, list) and ids and not isinstance(ids[0], int) \
                            and isinstance(ids[0], dict):
                        ids = [t.get("id") for t in ids]
                    clean = [int(i) for i in ids if i]
                    store.set_terms(con, row["id"], kind, field, clean)
                    tax_ids.setdefault(field, set()).update(clean)
            for tax, tids in tax_ids.items():
                try:
                    ensure_terms(con, tax, sorted(tids))
                except Exception as e:  # noqa: BLE001
                    log(f"  term backfill {tax} failed: {e}")
            seen += len(batch)
            log(f"  {kind}: page {page} -> {seen}/{total}")
            page += 1
            time.sleep(0.25)
    return counts


def scrape_terms(con) -> int:
    n = 0
    for alias, rest_base in up.TAXONOMIES.items():
        try:
            terms = up.all_terms(rest_base)
        except Exception as e:  # noqa: BLE001
            log(f"  taxonomy {rest_base} failed: {e}")
            continue
        for t in terms:
            store.upsert_term(con, rest_base, t)
        n += len(terms)
        log(f"  {rest_base}: {len(terms)} terms")
        time.sleep(0.2)
    return n


def _enrich_one(args) -> tuple:
    kind, item_id, link = args
    try:
        raw, _ = up.fetch(link)
        html_text = raw.decode("utf-8", "replace")
        detail = up.parse_detail(html_text)
        # Many posts keep their links only in the theme's #download block,
        # outside the REST content field - capture them here too.
        detail["downloads"] = ads.parse_page_downloads(html_text)
        return kind, item_id, detail, None
    except Exception as e:  # noqa: BLE001
        return kind, item_id, None, str(e)


def scrape_enrich(con, kinds: list[str], workers: int = 8, limit: int = 0) -> int:
    rows = con.execute(
        f"SELECT id, kind, link FROM items WHERE COALESCE(enriched,0)=0 "
        f"AND kind IN ({','.join('?' * len(kinds))})"
        + (f" LIMIT {int(limit)}" if limit else ""),
        kinds,
    ).fetchall()
    todo = [(r["kind"], r["id"], r["link"]) for r in rows if r["link"]]
    log(f"enrich: {len(todo)} pages to fetch, {workers} workers")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for kind, item_id, detail, err in pool.map(_enrich_one, todo):
            if err:
                continue
            cur = con.execute("SELECT downloads FROM items WHERE id=? AND kind=?",
                              (item_id, kind)).fetchone()
            have = []
            try:
                have = json.loads((cur["downloads"] if cur else "") or "[]")
            except (TypeError, ValueError):
                have = []
            new_dl = detail.get("downloads") or []
            with store._lock:
                if new_dl and not have:
                    con.execute(
                        "UPDATE items SET rating=?, rating_count=?, duration=?, release_date=?, "
                        "language=?, views=?, downloads=?, enriched=1 WHERE id=? AND kind=?",
                        (detail["rating"], detail.get("votes"), detail["duration"],
                         detail["release_date"], detail["language"], detail["views"],
                         json.dumps(new_dl, ensure_ascii=False), item_id, kind),
                    )
                else:
                    con.execute(
                        "UPDATE items SET rating=?, rating_count=?, duration=?, release_date=?, "
                        "language=?, views=?, enriched=1 WHERE id=? AND kind=?",
                        (detail["rating"], detail.get("votes"), detail["duration"],
                         detail["release_date"], detail["language"], detail["views"],
                         item_id, kind),
                    )
                con.commit()
            done += 1
            if done % 250 == 0:
                log(f"  enriched {done}/{len(todo)}")
    return done


def reclean(con) -> dict:
    """Re-apply ad/promo stripping and re-derive derived columns, offline.

    Stored content_html was stripped by whatever ads.py version was live at
    ingest time. After a strip/parse fix the stored rows keep the old output
    until something rewrites them, and a re-scrape would not help for content
    upstream no longer changes. Re-derives content_html, content_text and
    downloads from the stored HTML with no network access.
    """
    rows = con.execute(
        "SELECT id, kind, content_html, downloads FROM items").fetchall()
    n_html = n_dl = n_emptied = 0
    for r in rows:
        old_html = r["content_html"] or ""
        new_html = ads.strip_ads(old_html)
        try:
            old_dl = json.loads(r["downloads"] or "[]")
        except (TypeError, ValueError):
            old_dl = []
        has_marker = "Link Download" in new_html
        stale = any(not ads.is_download_url(l.get("url", ""))
                    for g in old_dl for l in g.get("links", []))
        new_dl = ads.parse_downloads(new_html) if (has_marker or stale) else old_dl
        html_changed = new_html != old_html
        dl_changed = json.dumps(new_dl, sort_keys=True) != json.dumps(old_dl, sort_keys=True)
        if not (html_changed or dl_changed):
            continue
        sets, args = [], []
        if html_changed:
            sets += ["content_html=?", "content_text=?"]
            args += [new_html, ads.text_of(new_html)]
        if dl_changed:
            sets += ["downloads=?"]
            args += [json.dumps(new_dl, ensure_ascii=False)]
        # Links went from present to empty: the page-level block was never
        # consulted for this row (the enrich stage skips rows that already had
        # downloads), so re-queue it or the record ends up with none.
        if old_dl and not new_dl:
            sets.append("enriched=0")
            n_emptied += 1
        args += [r["id"], r["kind"]]
        with store._lock:
            con.execute(f"UPDATE items SET {','.join(sets)} WHERE id=? AND kind=?", args)
            con.commit()
        n_html += int(html_changed)
        n_dl += int(dl_changed)
    return {"content": n_html, "downloads": n_dl, "requeued": n_emptied}


def main() -> int:
    p = argparse.ArgumentParser(description="Scrape web.kawanfilm21.co into SQLite")
    p.add_argument("--stage", default="all", choices=["core", "terms", "enrich", "all"])
    p.add_argument("--kinds", default="movie,tv,episode,page")
    p.add_argument("--db", default=store.DB_PATH)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, default=0, help="cap items per kind (0 = no cap)")
    p.add_argument("--redo-missing-downloads", action="store_true",
                   help="re-enrich items whose downloads list is still empty")
    p.add_argument("--reclean", action="store_true",
                   help="re-apply ad stripping + re-derive content/downloads offline")
    p.add_argument("--reparse-downloads", action="store_true",
                   help="alias for --reclean")
    p.add_argument("--stats", action="store_true")
    a = p.parse_args()

    con = store.connect(a.db)
    store.init(con)
    if a.stats:
        print(json.dumps(store.stats(con), indent=2))
        return 0

    if a.reclean or a.reparse_downloads:
        res = reclean(con)
        log(f"recleaned: content_html={res['content']} downloads={res['downloads']} "
            f"requeued_for_enrich={res['requeued']}")
        return 0

    if a.redo_missing_downloads:
        n = con.execute("UPDATE items SET enriched=0 WHERE downloads IS NULL OR downloads IN ('','[]')").rowcount
        con.commit()
        log(f"reset {n} items for re-enrichment")

    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    t0 = time.time()
    try:
        if a.stage in ("terms", "all"):
            log("scraping taxonomies...")
            n = scrape_terms(con)
            log(f"taxonomies done: {n} terms")
        if a.stage in ("core", "all"):
            log(f"scraping core items: {kinds}")
            counts = scrape_core(con, kinds)
            store.set_meta(con, "last_full_scrape", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            log(f"core done: {counts}")
        if a.stage in ("enrich", "all"):
            log("enriching detail fields...")
            n = scrape_enrich(con, kinds, workers=a.workers, limit=a.limit)
            log(f"enrich done: {n} items")
    except KeyboardInterrupt:
        log("interrupted - progress already committed")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    log(f"elapsed {time.time() - t0:.0f}s")
    print(json.dumps(store.stats(con), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
