#!/usr/bin/env python3
"""REST API for https://x6.sokuja.uk/ — scrape on demand, serve JSON.

Stdlib only. Run: python3 sokuja_api.py [port]
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "https://x6.sokuja.uk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
TTL = 300
ORIGIN = "https://x6.sokuja.uk"

_cache = {}
_lock = threading.Lock()


def fetch(path, timeout=40):
    url = path if path.startswith("http") else BASE + path
    key = url
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/json",
        "Referer": ORIGIN + "/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
        ctype = r.headers.get("Content-Type", "")
    with _lock:
        _cache[key] = (now, (body, ctype))
    return body, ctype


def flight(html):
    out = []
    for raw in re.findall(r"self\.__next_f\.push\((\[.*?\])\)</script>", html):
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list) and len(arr) >= 2 and isinstance(arr[1], str):
            out.append(arr[1])
    return "\n".join(out)


def json_arrays(text, marker):
    """Bracket-match every JSON array that starts at `marker`."""
    found = []
    for m in re.finditer(re.escape(marker), text):
        start = m.start()
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        data = None
                    if isinstance(data, list):
                        found.append(data)
                    break
    return found


def text_of(html):
    html = re.sub(r"<script[\s\S]*?</script>", " ", html)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html)
    html = re.sub(r"<br\s*/?>", "\n", html)
    html = re.sub(r"</p>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    html = html.replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    return re.sub(r"[ \t]+", " ", html)


def abs_url(src):
    if not src:
        return None
    src = src.replace("&amp;", "&")
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return ORIGIN + src
    return src


def img_src(block):
    """Real image URL: prefer a plain src, else the biggest srcset candidate."""
    m = re.search(r'\ssrc="(https?://[^"]+)"', block)
    if m:
        return m.group(1)
    srcs = re.findall(r"(?:srcSet|srcset)=\"([^\"]+)\"", block)
    best, best_w = None, -1
    for srcset in srcs:
        for part in srcset.replace("&amp;", "&").split(","):
            part = part.strip()
            mm = re.match(r"(\S+)\s+(\d+)w", part)
            if mm and int(mm.group(2)) > best_w:
                best, best_w = mm.group(1), int(mm.group(2))
    if best:
        q = urllib.parse.urlparse(best).query
        inner = urllib.parse.parse_qs(q).get("url", [None])[0]
        return abs_url(urllib.parse.unquote(inner)) if inner else abs_url(best)
    m = re.search(r'\ssrc="(/[^"]+)"', block)
    return abs_url(m.group(1)) if m else None


# ---------------------------------------------------------------- endpoints

def latest(page):
    html, _ = fetch(f"/?page={page}")
    big = flight(html)
    pat = re.compile(
        r'"ep-(\d+)",\{"prefetch":false,"href":"([^"]+)"'
        r'[\s\S]{0,900}?"src":"([^"]+)"'
        r'[\s\S]{0,200}?"alt":"([^"]*)"'
        r'[\s\S]{0,500}?"children":\["EP ","(\d+)"\]'
        r'(?:[\s\S]{0,300}?"children":"([A-Za-z]+)")?'
        r'[\s\S]{0,700}?"children":"((?:(?!"children")[\s\S]){1,160}?)"\}\]'
        r'[\s\S]{0,300}?"children":\["Episode ","\5"\]'
        r'(?:[\s\S]{0,160}?"children":\["· ","([^"]*)"\])?')
    items = []
    for m in pat.finditer(big):
        slug = m.group(2).strip("/")
        items.append({
            "id": int(m.group(1)),
            "slug": slug,
            "title": m.group(7).strip(),
            "episode": int(m.group(5)),
            "type": m.group(6),
            "releasedAgo": m.group(8),
            "thumbnail": abs_url(m.group(3)),
            "url": f"{ORIGIN}/{slug}/",
        })
    pages = [int(n) for n in set(re.findall(r"[?&]page=(\d+)", html))]
    return {
        "page": page,
        "perPage": len(items),
        "hasNext": (page + 1) in pages or len(items) > 0 and page == 1,
        "nextPage": page + 1 if items else None,
        "items": items,
    }


def search(q, limit):
    html, ctype = fetch(f"/api/search?q={urllib.parse.quote(q)}&limit={limit}")
    if "json" not in ctype:
        raise ValueError("search upstream failed")
    data = json.loads(html)
    for r in data.get("results", []):
        r["url"] = f"{ORIGIN}/anime/{r['slug']}/"
        for k in ("thumbnailUrl", "coverUrl"):
            r[k] = abs_url(r.get(k))
    return {"query": q, "count": len(data.get("results", [])),
            "results": data.get("results", [])}


def _info_block(html):
    m = re.search(r"<dl class=\"space-y-2\">([\s\S]*?)</dl>", html)
    info = {}
    if not m:
        return info
    for row in re.findall(r"<dt[^>]*>([\s\S]*?)</dt><dd[^>]*>([\s\S]*?)</dd>", m.group(1)):
        label = re.sub(r"<[^>]+>", "", row[0]).strip().lower()
        links = re.findall(r'href="(/[^"]+/)"[^>]*>([^<]+)', row[1])
        value = re.sub(r"<[^>]+>", " ", row[1])
        value = re.sub(r"\s+", " ", value).strip(" ,")
        entry = {"value": value}
        if links:
            entry["items"] = [{"name": n.strip(" ,"), "slug": u.strip("/").split("/")[-1],
                               "url": ORIGIN + u} for u, n in links]
        info[label] = entry
    return info


def _genres(html):
    m = re.search(r"(<a[^>]*href=\"/genre/[\s\S]*?)</div><div class=\"rounded-lg bg-gray-800/50", html)
    block = m.group(1) if m else ""
    out = []
    for slug, name in re.findall(r'href="/genre/([^"/]+)/"[^>]*>([^<]+)', block):
        out.append({"name": name.strip(), "slug": slug, "url": f"{ORIGIN}/genre/{slug}/"})
    return out


def _synopsis(html):
    m = re.search(r'class="prose prose-invert[\s\S]*?>([\s\S]*?)</div>', html)
    if not m:
        return None
    txt = text_of(m.group(1))
    return re.sub(r"\n{2,}", "\n", txt).strip()


def _anime_episodes(html):
    big = flight(html)
    best = []
    for arr in json_arrays(big, '[{"id":'):
        if arr and isinstance(arr[0], dict) and "episodeNumber" in arr[0] and len(arr) > len(best):
            best = arr
    eps = []
    for e in best:
        num = e.get("episodeNumber")
        created = e.get("createdAt")
        if isinstance(created, str) and created.startswith("$D"):
            created = created[2:]
        links = []
        for d in e.get("downloadLinks") or []:
            links.append({"id": d.get("id"), "title": d.get("title"),
                          "server": d.get("serverName"), "quality": d.get("quality"),
                          "url": d.get("url")})
        eps.append({"id": e.get("id"), "slug": e.get("slug"), "title": e.get("title"),
                     "episode": num, "createdAt": created, "downloads": links,
                     "url": f"{ORIGIN}/{e.get('slug')}/"})
    eps.sort(key=lambda x: (x["episode"] or 0), reverse=True)
    return eps


def anime_detail(slug):
    html, ctype = fetch(f"/anime/{slug}/")
    if "text/html" not in ctype or "Informasi Anime" not in html:
        return None
    title = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", html)
    title = re.sub(r"<[^>]+>", " ", title.group(1)) if title else slug
    title = re.sub(r"\s+", " ", title).replace("Subtitle Indonesia", "").strip()
    alt = re.search(r'"alternateName":"([^"]*)"', html)
    score = re.search(r'"ratingValue":([\d.]+)', html)
    votes = re.search(r'"ratingCount":(\d+)', html)
    cover = re.search(r'"image":"(https?://[^"]+)"', html)
    return {
        "slug": slug,
        "title": title,
        "alternateTitles": alt.group(1) if alt else None,
        "score": float(score.group(1)) if score else None,
        "scoreVotes": int(votes.group(1)) if votes else None,
        "cover": cover.group(1) if cover else None,
        "genres": _genres(html),
        "info": _info_block(html),
        "synopsis": _synopsis(html),
        "url": f"{ORIGIN}/anime/{slug}/",
        "episodes": _anime_episodes(html),
    }


def episode_detail(slug):
    html, ctype = fetch(f"/{slug}/")
    if "text/html" not in ctype or "video-player-area" not in html:
        return None
    big = flight(html)
    eid = re.search(r'"episodeId":(\d+)', big)
    episode_id = int(eid.group(1)) if eid else None
    title = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", html)
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title.group(1))).strip() if title else slug
    num = re.search(r'"episodeNumber":(\d+)', html)
    uploaded = re.search(r'"uploadDate":"([^"]+)"', html)
    thumb = re.search(r'"thumbnailUrl":"(https?://[^"]+)"', html)
    views = re.search(r">([\d.,]+K?)<!--\s*-->\s*views<", html)
    series = re.search(
        r'Informasi Series[\s\S]{0,1500}?href="(/anime/[a-z0-9-]+/)"[^>]*>\s*'
        r'<img alt="([^"]*)"', html)
    mirrors = []
    if episode_id:
        body, ct = fetch(f"/api/video-mirrors?e={episode_id}")
        if "json" in ct:
            for mirr in json.loads(body).get("mirrors", []):
                mirrors.append({"id": mirr.get("id"), "server": mirr.get("serverName"),
                                "quality": mirr.get("quality"), "type": mirr.get("embedType"),
                                "url": mirr.get("embedUrl")})
    downloads = []
    for href, label in re.findall(
            r'href="(https://sokuja\.id/x\.php\?[^"]+)"[\s\S]{0,400}?'
            r'font-semibold text-primary">([^<]+)</span>\s*Download', html):
        downloads.append({"quality": label.strip(), "url": href.replace("&amp;", "&")})
    nav = {}
    for key, rx in (("prev", r'"prev":\{"slug":"([^"]+)","title":"([^"]+)"'),
                     ("next", r'"next":\{"slug":"([^"]+)","title":"([^"]+)"')):
        m = re.search(rx, big)
        if m:
            nav[key] = {"slug": m.group(1), "title": m.group(2),
                        "url": f"{ORIGIN}/{m.group(1)}/"}
    return {
        "id": episode_id,
        "slug": slug,
        "title": title,
        "episode": int(num.group(1)) if num else None,
        "uploadedAt": uploaded.group(1) if uploaded else None,
        "views": views.group(1) if views else None,
        "thumbnail": thumb.group(1) if thumb else None,
        "series": {"slug": series.group(1).strip("/").split("/")[-1],
                    "title": series.group(2).strip(),
                    "url": ORIGIN + series.group(1)} if series else None,
        "stream": mirrors,
        "downloads": downloads,
        "navigation": nav,
        "url": f"{ORIGIN}/{slug}/",
    }


def _poster_grid(html):
    items = []
    for block in re.findall(r'<a class="group block" href="(/anime/[^"]+/)">([\s\S]*?)</a>', html):
        href, inner = block
        name = re.search(r'\salt="([^"]*)"', inner)
        badge = re.search(r"font-bold uppercase[^>]*>([^<]+)", inner)
        score = re.search(r">★?\s*([\d.]+)\s*<", inner)
        items.append({
            "slug": href.strip("/").split("/")[-1],
            "title": name.group(1).strip() if name else None,
            "type": badge.group(1).strip() if badge else None,
            "score": float(score.group(1)) if score else None,
            "thumbnail": img_src(inner),
            "url": ORIGIN + href,
        })
    return items


def browse(params):
    page = int(params.get("page", ["1"])[0] or 1)
    q = {"order": params.get("order", ["update"])[0]}
    for key in ("status", "type", "genre", "season", "studio", "search"):
        if params.get(key, [None])[0]:
            q[key] = params[key][0]
    if page > 1:
        q["page"] = str(page)
    html, _ = fetch("/anime/?" + urllib.parse.urlencode(q))
    items = _poster_grid(html)
    pages = sorted({int(n) for n in re.findall(r"[?&]page=(\d+)", html)})
    return {"filters": {k: v for k, v in q.items() if k != "page"},
            "page": page, "perPage": len(items),
            "hasNext": (page + 1) in pages, "items": items}


def genres():
    html, _ = fetch("/genre/")
    out, seen = [], set()
    rx = re.compile(
        r'href="/genre/([^"/]+)/">\s*<span[^>]*>([^<]+)</span>\s*'
        r'<span[^>]*>(\d+)</span>')
    for slug, name, count in rx.findall(html):
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name.strip(), "animeCount": int(count),
                     "url": f"{ORIGIN}/genre/{slug}/"})
    out.sort(key=lambda g: g["name"].lower())
    return {"count": len(out), "genres": out}


def genre_anime(slug, page):
    html, ctype = fetch(f"/genre/{slug}/?page={page}")
    if "text/html" not in ctype or "Daftar Anime" not in html:
        return None
    name = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", html)
    name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", name.group(1))).strip() if name else slug
    pages = sorted({int(n) for n in re.findall(r"[?&]page=(\d+)", html)})
    return {"slug": slug, "name": name, "page": page,
            "lastPage": max(pages) if pages else page,
            "hasNext": (page + 1) in pages, "items": _poster_grid(html)}


_DAYS = [("mon", "Senin"), ("tue", "Selasa"), ("wed", "Rabu"), ("thu", "Kamis"),
         ("fri", "Jumat"), ("sat", "Sabtu"), ("sun", "Minggu"),
         ("random", "Random / Belum Pasti"), ("libur", "Libur"),
         ("hiatus", "Hiatus"), ("end", "Sudah Selesai (END)")]


def schedule():
    html, _ = fetch("/jadwal-rilis-anime/")
    days = []
    for i, (sid, name) in enumerate(_DAYS):
        m = re.search(rf'id="{sid}"([\s\S]*?)(?=id="(?:' +
                      "|".join(s for s, _ in _DAYS[i + 1:]) + r')"|$)', html)
        region = m.group(1) if m else ""
        entries = []
        for block in re.findall(r'<a href="(/anime/[^"]+/)">([\s\S]*?)</a>', region):
            href, inner = block
            title = re.search(r'\salt="([^"]*)"', inner)
            when = re.search(r">(\d{1,2}:\d{2})(?:<!--\s*-->)?\s*([A-Z]{2,4})<", inner)
            kind = re.search(r"uppercase[^>]*>([A-Za-z]+)", inner)
            entries.append({
                "slug": href.strip("/").split("/")[-1],
                "title": title.group(1).strip() if title else None,
                "time": when.group(1) if when else None,
                "timezone": when.group(2) if when else None,
                "type": kind.group(1) if kind else None,
                "thumbnail": img_src(inner),
                "url": ORIGIN + href,
            })
        days.append({"id": sid, "day": name, "count": len(entries), "anime": entries})
    return {"timezone": "WIB", "days": days}


ROUTES = {
    "GET /": "service info",
    "GET /api/latest?page=1": "episode terbaru (18/halaman)",
    "GET /api/search?q=one%20piece&limit=10": "cari anime",
    "GET /api/anime/{slug}": "detail anime + sinopsis + seluruh episode",
    "GET /api/episode/{slug}": "detail episode + stream + link download",
    "GET /api/schedule": "jadwal rilis per hari",
    "GET /api/genres": "daftar genre",
    "GET /api/genre/{slug}?page=1": "anime dalam satu genre",
    "GET /api/browse?status=ongoing&type=tv&page=1": "telusuri: status, type, order, page",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=120")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)
        try:
            self._route(path, q)
        except urllib.error.HTTPError as e:
            self._send(e.code, {"error": f"upstream {e.code}"})
        except Exception as e:  # noqa: BLE001
            self._send(502, {"error": str(e)})

    def _route(self, path, q):
        if path == "/":
            return self._send(200, {"service": "sokuja-api", "source": ORIGIN,
                                     "cacheTtlSeconds": TTL, "endpoints": ROUTES})
        if path == "/api/latest":
            page = max(1, int(q.get("page", ["1"])[0] or 1))
            return self._send(200, latest(page))
        if path == "/api/search":
            query = q.get("q", [""])[0].strip()
            if not query:
                return self._send(400, {"error": "parameter q wajib diisi"})
            limit = min(30, max(1, int(q.get("limit", ["10"])[0] or 10)))
            return self._send(200, search(query, limit))
        if path == "/api/schedule":
            return self._send(200, schedule())
        if path == "/api/genres":
            return self._send(200, genres())
        if path == "/api/browse":
            return self._send(200, browse(q))
        m = re.fullmatch(r"/api/anime/([a-z0-9-]+)", path)
        if m:
            data = anime_detail(m.group(1))
            return self._send(200, data) if data else self._send(404, {"error": "anime tidak ditemukan"})
        m = re.fullmatch(r"/api/episode/([a-z0-9-]+)", path)
        if m:
            data = episode_detail(m.group(1))
            return self._send(200, data) if data else self._send(404, {"error": "episode tidak ditemukan"})
        m = re.fullmatch(r"/api/genre/([a-z0-9-]+)", path)
        if m:
            page = max(1, int(q.get("page", ["1"])[0] or 1))
            data = genre_anime(m.group(1), page)
            return self._send(200, data) if data else self._send(404, {"error": "genre tidak ditemukan"})
        self._send(404, {"error": "endpoint tidak dikenal", "endpoints": ROUTES})


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"sokuja-api listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
