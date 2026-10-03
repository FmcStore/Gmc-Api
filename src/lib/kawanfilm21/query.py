"""JSON query layer over the scraped KawanFilm21 data."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse

import ads
import store
import upstream as up

KINDS = ("movie", "tv", "episode", "page")
TERM_ALIASES = {
    "genre": "categories", "genres": "categories", "tag": "tags", "tags": "tags",
    "director": "muvidirector", "directors": "muvidirector", "cast": "muvicast",
    "year": "muviyear", "years": "muviyear", "country": "muvicountry",
    "countries": "muvicountry", "quality": "muviquality", "qualities": "muviquality",
    "index": "muviindex", "indexes": "muviindex", "network": "muvinetwork",
    "networks": "muvinetwork",
}
FILTERS = ("categories", "tags", "muvidirector", "muvicast", "muviyear",
           "muvicountry", "muviquality", "muviindex", "muvinetwork")
# Local taxonomy name -> key used in the JSON output.
OUT_KEYS = {
    "categories": "genres", "tags": "tags", "muvidirector": "directors",
    "muvicast": "cast", "muviyear": "years", "muvicountry": "countries",
    "muviquality": "qualities", "muviindex": "indexes", "muvinetwork": "networks",
}
ORDERBY = {"date": "date", "title": "title", "id": "id", "rating": "rating",
           "views": "views", "modified": "modified"}

_CACHE: dict = {}
_CACHE_TTL = 60


def cache_get(key):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def cache_put(key, value):
    if len(_CACHE) > 512:
        _CACHE.clear()
    _CACHE[key] = (time.time(), value)
    return value


class NotFound(Exception):
    pass


def _json_col(v, default):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def terms_for(con, kind: str, item_ids: list) -> dict:
    """item_id -> {out_key: [term,...]}"""
    out = {i: {} for i in item_ids}
    if not item_ids:
        return out
    ph = ",".join("?" * len(item_ids))
    rows = con.execute(
        f"SELECT it.item_id, it.taxonomy, t.id, t.name, t.slug, t.link, t.count "
        f"FROM item_terms it JOIN terms t ON t.id=it.term_id AND t.taxonomy=it.taxonomy "
        f"WHERE it.item_kind=? AND it.item_id IN ({ph}) ORDER BY t.name",
        [kind] + list(item_ids),
    ).fetchall()
    for r in rows:
        key = OUT_KEYS.get(r["taxonomy"], r["taxonomy"])
        out.setdefault(r["item_id"], {}).setdefault(key, []).append(
            {"id": r["id"], "name": r["name"], "slug": r["slug"], "link": r["link"]})
    return out


def serialize(con, row: sqlite3.Row, terms_map: dict, *, content: bool = False,
              content_mode: str = "ads") -> dict:
    d = dict(row)
    dl = _json_col(d.get("downloads"), [])
    html = d.get("content_html") or ""
    if content and content_mode == "aggressive":
        html = ads.strip_spam(html)
    item = {
        "id": d["id"],
        "kind": d["kind"],
        "title": d.get("title"),
        "slug": d.get("slug"),
        "url": d.get("link"),
        "date": d.get("date"),
        "modified": d.get("modified"),
        "poster": d.get("poster"),
        "poster_thumbnail": d.get("poster_small"),
        "synopsis": d.get("synopsis") or "",
        "excerpt": d.get("excerpt") or "",
        "rating": d.get("rating"),
        "rating_count": d.get("rating_count"),
        "duration": d.get("duration"),
        "release_date": d.get("release_date"),
        "language": d.get("language"),
        "views": d.get("views"),
        "downloads": dl,
        "download_groups": len(dl),
        "download_links": sum(len(g.get("links", [])) for g in dl),
        "detail_enriched": bool(d.get("enriched")),
        "ad_free": True,
    }
    item.update(terms_map.get(d["id"], {}))
    for k in OUT_KEYS.values():
        item.setdefault(k, [])
    if content:
        item["content_html"] = html
        item["content_text"] = d.get("content_text") or ""
    return item


def _item_rows(con, kind: str, *, page=1, per_page=20, search=None, orderby="date",
               order="desc", terms=None, ids=None):
    where, args = ["kind=?"], [kind]
    if search:
        where.append("(title LIKE ? OR synopsis LIKE ? OR content_text LIKE ?)")
        args += [f"%{search}%"] * 3
    if ids is not None:
        if not ids:
            return [], 0
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args += list(ids)
    for taxonomy, values in (terms or {}).items():
        if not values:
            continue
        conds, cargs = [], []
        for v in values:
            if str(v).isdigit():
                conds.append("(t.id=? OR t.slug=?)")
                cargs += [int(v), str(v)]
            else:
                conds.append("t.slug=?")
                cargs += [str(v)]
        where.append(
            "id IN (SELECT item_id FROM item_terms it JOIN terms t "
            "ON t.id=it.term_id AND t.taxonomy=it.taxonomy "
            f"WHERE it.item_kind=? AND it.taxonomy=? AND ({' OR '.join(conds)}))")
        args += [kind, taxonomy] + cargs
    clause = " AND ".join(where)
    total = con.execute(f"SELECT COUNT(*) c FROM items WHERE {clause}", args).fetchone()["c"]
    ob = ORDERBY.get(orderby, "date")
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    nulls = f" ORDER BY ({ob} IS NULL), {ob} {direction}" if ob in ("rating", "views") else f" ORDER BY {ob} {direction}"
    rows = con.execute(
        f"SELECT * FROM items WHERE {clause}{nulls} LIMIT ? OFFSET ?",
        args + [per_page, (page - 1) * per_page],
    ).fetchall()
    return rows, total


def envelope(page, per_page, total, results, extra=None):
    import math
    tp = max(1, math.ceil(total / per_page)) if total else 0
    out = {
        "source": up.ORIGIN,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": tp,
        "count": len(results),
        "has_next": page * per_page < total,
        "has_prev": page > 1,
        "results": results,
    }
    if extra:
        out.update(extra)
    return out


def list_items(con, kind, q) -> dict:
    page = max(1, int(q.get("page", [1])[0]))
    per_page = min(100, max(1, int(q.get("per_page", [20])[0])))
    orderby = q.get("orderby", ["date"])[0]
    order = q.get("order", ["desc"])[0]
    search = (q.get("search") or q.get("q") or [None])[0]
    want_content = (q.get("content", ["0"])[0] in ("1", "true", "yes"))
    cmode = (q.get("clean", ["ads"])[0] or "ads").lower()
    terms = {}
    for alias, canon in TERM_ALIASES.items():
        if alias in q and q[alias]:
            vals = []
            for raw in q[alias]:
                vals.extend([v for v in str(raw).split(",") if v])
            terms.setdefault(canon, []).extend(vals)
    # A term slug passed to the taxonomy's own filter is also accepted.
    rows, total = _item_rows(con, kind, page=page, per_page=per_page, search=search,
                             orderby=orderby, order=order, terms=terms)
    tmap = terms_for(con, kind, [r["id"] for r in rows])
    results = [serialize(con, r, tmap, content=want_content, content_mode=cmode) for r in rows]
    return envelope(page, per_page, total, results,
                    {"kind": kind, "filters": {k: v for k, v in terms.items() if v}})


def get_item(con, kind, ident, q) -> dict:
    want_content = (q.get("content", ["1"])[0] not in ("0", "false", "no"))
    cmode = (q.get("clean", ["ads"])[0] or "ads").lower()
    if str(ident).isdigit():
        row = con.execute("SELECT * FROM items WHERE kind=? AND id=?", (kind, int(ident))).fetchone()
    else:
        row = con.execute("SELECT * FROM items WHERE kind=? AND slug=?", (kind, ident)).fetchone()
    if row is None:
        raise NotFound(f"{kind} '{ident}' not found")
    tmap = terms_for(con, kind, [row["id"]])
    item = serialize(con, row, tmap, content=want_content, content_mode=cmode)
    if kind == "tv":
        eps = con.execute(
            "SELECT id,title,slug,link,date FROM items WHERE kind='episode' AND title LIKE ? "
            "ORDER BY id ASC LIMIT 500", (f"%{_series_key(row['title'])}%",)).fetchall()
        item["episodes"] = [dict(e) for e in eps]
        item["episode_count"] = len(eps)
    return item


def _series_key(title: str) -> str:
    import re
    return re.sub(r"\s*\(\d{4}\)\s*$", "", (title or "").strip())


def list_terms(con, taxonomy, q) -> dict:
    page = max(1, int(q.get("page", [1])[0]))
    per_page = min(500, max(1, int(q.get("per_page", [100])[0])))
    orderby = {"count": "count", "name": "name", "id": "id"}.get(q.get("orderby", ["name"])[0], "name")
    direction = "ASC" if q.get("order", ["asc"])[0].lower() == "asc" else "DESC"
    search = (q.get("search") or q.get("q") or [None])[0]
    where, args = ["taxonomy=?"], [taxonomy]
    if search:
        where.append("name LIKE ?")
        args.append(f"%{search}%")
    clause = " AND ".join(where)
    total = con.execute(f"SELECT COUNT(*) c FROM terms WHERE {clause}", args).fetchone()["c"]
    rows = con.execute(
        f"SELECT id,name,slug,link,count,parent,description FROM terms WHERE {clause} "
        f"ORDER BY ({orderby} IS NULL), {orderby} {direction} LIMIT ? OFFSET ?",
        args + [per_page, (page - 1) * per_page]).fetchall()
    results = [{"id": r["id"], "name": r["name"], "slug": r["slug"], "link": r["link"],
                "count": r["count"], "parent": r["parent"],
                "description": ads.text_of(r["description"] or "")} for r in rows]
    return envelope(page, per_page, total, results, {"taxonomy": taxonomy})


def term_items(con, taxonomy, ident, q) -> dict:
    if str(ident).isdigit():
        t = con.execute("SELECT * FROM terms WHERE taxonomy=? AND id=?",
                        (taxonomy, int(ident))).fetchone()
    else:
        t = con.execute("SELECT * FROM terms WHERE taxonomy=? AND slug=?",
                        (taxonomy, ident)).fetchone()
    if t is None:
        raise NotFound(f"{taxonomy} '{ident}' not found")
    kind = q.get("kind", ["movie"])[0]
    page = max(1, int(q.get("page", [1])[0]))
    per_page = min(100, max(1, int(q.get("per_page", [20])[0])))
    rows, total = _item_rows(con, kind, page=page, per_page=per_page,
                             terms={taxonomy: [t["id"]]},
                             orderby=q.get("orderby", ["date"])[0],
                             order=q.get("order", ["desc"])[0])
    tmap = terms_for(con, kind, [r["id"] for r in rows])
    res = envelope(page, per_page, total, [serialize(con, r, tmap) for r in rows])
    res["term"] = {"id": t["id"], "taxonomy": taxonomy, "name": t["name"],
                   "slug": t["slug"], "count": t["count"], "kind": kind}
    return res


def search(con, q) -> dict:
    query = (q.get("q") or q.get("search") or [""])[0]
    if not query:
        return envelope(0, 0, 0, [], {"error": "missing_query", "hint": "pass ?q=..."})
    page = max(1, int(q.get("page", [1])[0]))
    per_page = min(100, max(1, int(q.get("per_page", [20])[0])))
    kind = q.get("kind", [None])[0]
    # Search matches the body, so results may carry no visible match in the
    # short fields; honour ?content=1 so callers can show where it matched.
    want_content = (q.get("content", ["0"])[0] in ("1", "true", "yes"))
    cmode = (q.get("clean", ["ads"])[0] or "ads").lower()
    kinds = [kind] if kind else list(KINDS)
    all_rows = []
    for k in kinds:
        rows, _ = _item_rows(con, k, page=1, per_page=1000, search=query)
        all_rows.extend(rows)
    total = len(all_rows)
    start = (page - 1) * per_page
    window = all_rows[start:start + per_page]
    bykind: dict = {}
    for r in window:
        bykind.setdefault(r["kind"], []).append(r["id"])
    tmap = {}
    for k, ids in bykind.items():
        tmap.update(terms_for(con, k, ids))
    results = [serialize(con, r, tmap, content=want_content, content_mode=cmode)
               for r in window]
    return envelope(page, per_page, total, results, {"query": query})


def downloads(con, kind, ident) -> dict:
    item = get_item(con, kind, ident, {"content": ["0"]})
    flat = [{"quality": g["quality"], "host": l["host"], "url": l["url"]}
            for g in item["downloads"] for l in g["links"]]
    return {"id": item["id"], "title": item["title"], "slug": item["slug"],
            "url": item["url"], "groups": item["downloads"],
            "flat": flat, "total": len(flat), "ad_free": True}
