"""Upstream client for web.kawanfilm21.co (WordPress + muvipro theme)."""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = "https://web.kawanfilm21.co"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# WP REST collection endpoints -> our item kind.
ENDPOINTS = {"movie": "posts", "tv": "tv", "episode": "episode", "page": "pages"}
TAXONOMIES = {
    "genre": "categories", "tag": "tags", "director": "muvidirector",
    "cast": "muvicast", "year": "muviyear", "country": "muvicountry",
    "quality": "muviquality", "index": "muviindex", "network": "muvinetwork",
}
# taxonomy rest_base -> the field name WP puts on the item object
TERM_FIELDS = {
    "categories": "categories", "tags": "tags", "muvidirector": "muvidirector",
    "muvicast": "muvicast", "muviyear": "muviyear", "muvicountry": "muvicountry",
    "muviquality": "muviquality", "muviindex": "muviindex", "muvinetwork": "muvinetwork",
}


class UpstreamError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


def fetch(url: str, timeout: int = 45, retries: int = 3) -> tuple[bytes, dict]:
    """GET a URL, returning (body, headers). Retries transient failures."""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json,text/html,*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw, dict(resp.headers)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            last = UpstreamError(e.code, url, body)
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last
        except Exception as e:  # noqa: BLE001 - network flake
            last = UpstreamError(0, url, str(e))
            time.sleep(1.5 * (attempt + 1))
    raise last


def fetch_json(url: str, timeout: int = 45) -> tuple[object, dict]:
    raw, headers = fetch(url, timeout=timeout)
    return json.loads(raw.decode("utf-8", "replace")), headers


def api(path: str, **params) -> tuple[object, dict]:
    url = f"{ORIGIN}/wp-json/wp/v2/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return fetch_json(url)


def count(kind: str) -> int:
    """Total records for an item kind (from X-WP-Total)."""
    _, headers = api(ENDPOINTS[kind], per_page=1, _fields="id")
    return int(headers.get("X-WP-Total", 0))


def page_items(kind: str, page: int, per_page: int = 100) -> list:
    data, _ = api(ENDPOINTS[kind], page=page, per_page=per_page, orderby="id", order="asc",
                  _fields="id,slug,link,title,excerpt,content,date,modified,"
                          "featured_media,categories,tags,muvidirector,muvicast,"
                          "muviyear,muvicountry,muviquality,muviindex,muvinetwork")
    return data if isinstance(data, list) else []


def media_urls(ids: list) -> dict:
    """id -> {"full": url, "thumb": url} for up to 100 media ids.

    `include` MUST be comma-joined: WP_REST_Request keeps only the last value
    of a repeated bare param (include=1&include=2 -> just 2), so a list here
    silently returns one row.
    """
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    data, _ = api("media", include=",".join(str(i) for i in ids[:100]), per_page=100,
                  _fields="id,source_url,media_details")
    out = {}
    for m in data if isinstance(data, list) else []:
        det = m.get("media_details") or {}
        sizes = det.get("sizes") or {}
        thumb = (sizes.get("medium") or {}).get("source_url") or m.get("source_url")
        out[int(m["id"])] = {"full": m.get("source_url"), "thumb": thumb}
    return out


def terms_by_include(rest_base: str, ids: list) -> list:
    """Fetch specific term ids (comma-joined, see media_urls for why)."""
    ids = [int(i) for i in ids if i][:100]
    if not ids:
        return []
    data, _ = api(rest_base, include=",".join(str(i) for i in ids), per_page=100,
                  _fields="id,name,slug,link,parent,count,description")
    return data if isinstance(data, list) else []


def all_terms(rest_base: str, per_page: int = 100) -> list:
    """Every term of a taxonomy, following X-WP-TotalPages.

    A 522 from Cloudflare on one page must not truncate the whole taxonomy:
    retry that page with a long backoff, and if it still fails skip it and keep
    walking. Item-level backfill (scrape.ensure_terms) fills any real gap.
    """
    out, page, failed = [], 1, []
    total_pages = None
    while True:
        data = None
        for attempt in range(4):
            try:
                data, headers = api(rest_base, page=page, per_page=per_page,
                                    orderby="id", order="asc",
                                    _fields="id,name,slug,link,parent,count,description")
                if total_pages is None:
                    total_pages = int(headers.get("X-WP-TotalPages", 1))
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    failed.append(page)
                    data = None
                    break
                time.sleep(5 * (attempt + 1))
        if data is None:
            page += 1
            if total_pages and page > total_pages:
                break
            continue
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if total_pages and page >= total_pages:
            break
        page += 1
    if failed:
        print(f"  ! {rest_base}: pages failed after retries: {failed}", flush=True)
    return out


# ---------------------------------------------------------------- detail page
# The main movie's rating lives in the aggregateRating block, NOT in the first
# gmr-rating-item on the page: those belong to the "Film Terkait" cards and
# reading them gives a neighbouring film's score.
_META_RE = re.compile(
    r'<div class="gmr-moviedata[^"]*">\s*<strong>(.*?)</strong>\s*(.*?)\s*</div>', re.I | re.S)
_RATING_RES = (
    re.compile(r'itemprop="ratingValue"[^>]*content="([0-9]+(?:\.[0-9]+)?)"', re.I),
    re.compile(r'itemprop="ratingValue"[^>]*>\s*([0-9]+(?:\.[0-9]+)?)', re.I),
)
_VOTES_RES = (
    re.compile(r'itemprop="ratingCount"[^>]*content="([\d.,]+)"', re.I),
    re.compile(r'itemprop="ratingCount"[^>]*>\s*([\d.,]+)', re.I),
)
_VIEWS_RE = re.compile(r'gmr-movie-view[^>]*>.*?([\d.,]+)\s*views', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    import html as _h
    return " ".join(_h.unescape(_TAG_RE.sub("", s or "")).split())


def parse_detail(html_text: str) -> dict:
    """Extract rating/duration/release/language/views from a single post page."""
    out = {"rating": None, "votes": None, "duration": None,
           "release_date": None, "language": None, "views": None}
    for rx in _RATING_RES:
        m = rx.search(html_text or "")
        if m:
            try:
                out["rating"] = float(m.group(1))
                break
            except ValueError:
                pass
    for rx in _VOTES_RES:
        m = rx.search(html_text or "")
        if m:
            try:
                out["votes"] = int(re.sub(r"[^\d]", "", m.group(1)) or 0)
                break
            except ValueError:
                pass
    v = _VIEWS_RE.search(html_text or "")
    if v:
        try:
            out["views"] = int(re.sub(r"[^\d]", "", v.group(1)) or 0)
        except ValueError:
            pass
    for label, value in _META_RE.findall(html_text or ""):
        key = _clean(label).rstrip(":").lower()
        val = _clean(value)
        if not val:
            continue
        if key.startswith("durasi"):
            out["duration"] = val
        elif key.startswith("rilis"):
            out["release_date"] = val
        elif key.startswith("bahasa"):
            out["language"] = val
    return out
