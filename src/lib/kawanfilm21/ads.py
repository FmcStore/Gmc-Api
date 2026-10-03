"""Ad / promo stripping and content parsing for KawanFilm21 payloads.

The site injects ads in three places:
  1. Template containers outside the post body  -> header-banner, #floatads,
     #floating_ads_bottom_textcss_wrap, #timeloading (interstitial overlay).
     These never reach the REST payload, so they only matter when the raw
     page HTML is scraped.
  2. <script>/<ins>/<iframe> ad tags -> removed everywhere.
  3. In-content promo blocks repeated verbatim on every single post:
     the "TUTORIAL CARA DOWNLOAD" banner and the "GRUP TELEGRAM" banner.
     These are self-promotion, i.e. ads, and are dropped by default.

The PERHATIAN info box is a playback notice, not an ad, so it is kept.
"""
from __future__ import annotations

import html
import re

# Template-level ad containers (page HTML only).
AD_CONTAINER_IDS = (
    "floatads", "close-floatads", "timeloading", "timeloading-wrap",
    "timeloading-noclick", "floating_ads_bottom_textcss_wrap",
    "floating_ads_bottom_textcss_ad",
)
AD_CONTAINER_CLASSES = ("header-banner", "gmr-banner", "ad-slot", "adsbygoogle")

_SCRIPT_RE = re.compile(r"<(script|ins|iframe|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_SELFCLOSE_RE = re.compile(r"<(script|ins|iframe|embed|object)\b[^>]*/?>", re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# In-content promo banners, anchored on their unique link/text.
_PROMO_RES = (
    # TUTORIAL CARA DOWNLOAD banner
    re.compile(r"<div[^>]*>\s*<strong>\s*<a[^>]*cara-download-film-di-kawanfilm21[^>]*>.*?</a>\s*</strong>\s*</div>", re.I | re.S),
    # GRUP TELEGRAM banner
    re.compile(r"<div[^>]*>(?:(?!</div>).)*?t\.me/kawanfilm21grup(?:(?!</div>).)*?</div>", re.I | re.S),
)
# The site's own promo/self-ad channels. A block that links to nothing else is
# a promo block, whatever markup wraps it (banner <div> or prose <p>).
_PROMO_URLS = ("t.me/kawanfilm21grup", "cara-download-film-di-kawanfilm21",
               "pasang-iklan-murah")
_P_BLOCK_RE = re.compile(r"<p\b[^>]*>.*?</p>", re.I | re.S)


def _is_promo_block(block: str) -> bool:
    """True when every link in the block points at a promo channel."""
    anchors = ANCHOR_RE.findall(block)
    if not anchors:
        return False
    promo = 0
    for url, label in anchors:
        blob = (url + " " + label).lower()
        if any(p in blob for p in _PROMO_URLS):
            promo += 1
    return promo == len(anchors)


def _drop_promo_paragraphs(out: str) -> str:
    return _P_BLOCK_RE.sub(lambda m: "" if _is_promo_block(m.group(0)) else m.group(0), out)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")

DL_HEADER_RE = re.compile(r"Link Download[^<]*", re.I)
QUALITY_RE = re.compile(r"^\s*(\d{3,4}p\s*(?:MP4|MKV|WEB-?DL)?|Batch[^<\n]*)\s*$", re.I)
ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_STRIP_TAGS = ("script", "style", "iframe", "ins", "noscript", "form")

# Internal site URLs are navigation, not downloads. Only allow an internal URL
# when it points at a real uploaded file.
_OWN_HOSTS = ("kawanfilm21.co",)
_FILE_PATH_OK = ("/wp-content/uploads/",)
# Social / community / promo / CDN hosts: never file hosts. A promo anchor that
# reaches the parser (a Telegram invite, a banner image) must not be served as a
# download link, and it keeps the row "clean" so later passes skip it.
_BLOCKED_HOSTS = ("t.me", "telegram.me", "telegram.org", "whatsapp.com",
                  "facebook.com", "fb.com", "instagram.com", "twitter.com",
                  "x.com", "youtube.com", "youtu.be", "blogger.com",
                  "blogspot.com", "googleusercontent.com")


def is_download_url(url: str) -> bool:
    """True if `url` looks like an actual external download link."""
    import urllib.parse as _u
    try:
        p = _u.urlparse(url)
    except ValueError:
        return False
    host = p.netloc.lower().split(":")[0]
    # Reject scheme-as-host artifacts ("https://https://site/") and anything
    # without a real dotted domain - a file host always has one.
    if not host or "." not in host or host in ("http", "https"):
        return False
    if any(host == h or host.endswith("." + h) for h in _BLOCKED_HOSTS):
        return False
    if any(host == h or host.endswith("." + h) for h in _OWN_HOSTS):
        return p.path.startswith(_FILE_PATH_OK)
    return True


def strip_ads(raw_html: str) -> str:
    """Remove ad tags and the repeated self-promo banners from a payload."""
    if not raw_html:
        return ""
    out = _SCRIPT_RE.sub("", raw_html)
    out = _SELFCLOSE_RE.sub("", out)
    out = _COMMENT_RE.sub("", out)
    for rx in _PROMO_RES:
        out = rx.sub("", out)
    out = _drop_promo_paragraphs(out)
    # Drop whole template ad containers if present (raw page HTML).
    for key in AD_CONTAINER_IDS:
        out = re.sub(
            r'<div[^>]*id="%s"[^>]*>.*?</div>\s*</div>' % re.escape(key), "", out, flags=re.I | re.S
        )
    for key in AD_CONTAINER_CLASSES:
        out = re.sub(
            r'<div[^>]*class="[^"]*%s[^"]*"[^>]*>.*?</div>' % re.escape(key), "", out, flags=re.I | re.S
        )
    return out.strip()


def text_of(raw_html: str) -> str:
    """Plain text of an HTML fragment, with entities decoded."""
    out = _SCRIPT_RE.sub("", raw_html or "")
    out = re.sub(r"<br\s*/?>|</p>|</div>", "\n", out, flags=re.I)
    out = _TAG_RE.sub("", out)
    out = html.unescape(out)
    out = _WS_RE.sub(" ", out)
    out = "\n".join(line.strip() for line in out.splitlines())
    return _BLANK_RE.sub("\n\n", out).strip()


def parse_downloads(raw_html: str) -> list[dict]:
    """Turn the 'Link Download' block into structured rows.

    Returns [{"quality": "480p MP4", "links": [{"host": "Google Drive",
    "url": "..."}]}]. Rows whose quality cannot be read are labelled "other".

    Anchors must be read BEFORE tags are stripped - flattening the fragment to
    text first destroys every href and yields zero downloads.
    """
    cleaned = strip_ads(raw_html or "")
    if not cleaned:
        return []
    # Line boundaries first, so each row stays on its own line.
    flat = re.sub(r"<br\s*/?>|</p>|</div>|</strong>", "\n", cleaned, flags=re.I)
    m = re.search(r"Link Download", flat, re.I)
    if not m:
        # No download header in the body: this post keeps its links in the
        # theme's #download block instead (see parse_page_downloads). Without
        # the guard, stray body anchors get read as downloads.
        return []
    flat = flat[m.start():]

    rows: list[dict] = []
    current: dict | None = None
    for line in flat.splitlines():
        line = line.strip()
        if not line:
            continue
        anchors = ANCHOR_RE.findall(line)
        plain = text_of(line)
        if anchors:
            if current is None:
                current = {"quality": "other", "links": []}
                rows.append(current)
            for url, label in anchors:
                url = html.unescape(url).strip()
                if not url.startswith(("http://", "https://")):
                    continue
                if not is_download_url(url):
                    continue
                host = text_of(label) or "Link"
                if any(l["url"] == url for l in current["links"]):
                    continue
                current["links"].append({"host": host, "url": url})
            continue
        qm = QUALITY_RE.match(plain)
        if qm:
            current = {"quality": qm.group(1).strip(), "links": []}
            rows.append(current)
    return [r for r in rows if r["links"]]


# Two heading styles exist: newer posts say "Berikut Sinopsis <title> (year):",
# older ones say "Sinopsis <title> (year) :".
_SYNOPSIS_RES = (
    re.compile(r"Berikut Sinopsis[^:\n]*:\s*(.+?)(?:\n\n|\Z)", re.I | re.S),
    re.compile(r"Sinopsis\s+.*?\([12]\d{3}\)\s*:\s*(.+?)(?:\n\n|\Z)", re.I | re.S),
    re.compile(r"^Sinopsis\s*:\s*(.+?)(?:\n\n|\Z)", re.I | re.M | re.S),
)


def parse_synopsis(raw_html: str) -> str:
    """Pull the sinopsis paragraph out of the body text."""
    txt = text_of(strip_ads(raw_html or ""))
    for rx in _SYNOPSIS_RES:
        m = rx.search(txt)
        if m:
            out = " ".join(m.group(1).split())
            if len(out) > 20:
                return out
    return ""


# ------------------------------------------------- second download format
# Posts whose body carries only a synopsis keep their links in the theme block
#   <div id="download" class="gmr-download-wrap">
#     <ul class="list-inline gmr-download-list"> <li><a href=... >SVG Host 1080p</a>
# Each <li> is one host+quality; the label is inside the anchor after the icon.
_PAGE_DL_UL_RE = re.compile(r'<ul[^>]*class="[^"]*gmr-download-list[^"]*"[^>]*>(.*?)</ul>',
                            re.I | re.S)
_DL_ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_SVG_RE = re.compile(r"<svg.*?</svg>", re.I | re.S)
_QUAL_RE = re.compile(r"(\d{3,4}p)", re.I)


def parse_page_downloads(page_html: str) -> list[dict]:
    """Download rows from the theme's gmr-download-list block."""
    grouped: dict = {}
    order: list = []
    for block in _PAGE_DL_UL_RE.findall(page_html or ""):
        for url, inner in _DL_ANCHOR_RE.findall(block):
            url = html.unescape(url).strip()
            if not url.startswith(("http://", "https://")):
                continue
            if not is_download_url(url):
                continue
            label = text_of(_SVG_RE.sub("", inner))
            if not label:
                continue
            qm = _QUAL_RE.search(label)
            quality = qm.group(1).lower() if qm else "other"
            host = _QUAL_RE.sub("", label).strip(" -|,–") or label
            if quality not in grouped:
                grouped[quality] = {"quality": quality, "links": []}
                order.append(quality)
            if not any(l["url"] == url for l in grouped[quality]["links"]):
                grouped[quality]["links"].append({"host": host, "url": url})
    return [grouped[q] for q in order if grouped[q]["links"]]


# Older posts carry boilerplate paragraphs that are pure SEO keyword spam
# ("Nonton Streaming Download Film <title> Sub Indo HD ... Kawanfilm21" and a
# long site-name dump). They are promo filler, not film detail, so only the
# aggressive clean drops them.
_SPAM_HINTS = ("gudangmovies", "layarkaca", "indoxxi", "zonafilm", "ganool",
               "pusatfilm21", "savefilm21", "lk21", "dunia21", "fmoviez")
_DL_KEYWORDS = ("google drive", "openload", "zippyshare", "mediafire", "rapidvideo")


def strip_spam(raw_html: str) -> str:
    """Drop <p> blocks that are SEO keyword dumps about other sites."""
    if not raw_html:
        return ""
    out, dropped = [], 0
    for block in re.split(r"(?=<p\b)", raw_html):
        low = block.lower()
        if block.lstrip().lower().startswith("<p") and len(block) > 120:
            hits = sum(1 for h in _SPAM_HINTS if h in low)
            dl = sum(1 for k in _DL_KEYWORDS if k in low)
            links = len(ANCHOR_RE.findall(block))
            if hits >= 3 or (dl >= 4 and links == 0):
                dropped += 1
                continue
        out.append(block)
    return "".join(out).strip()

