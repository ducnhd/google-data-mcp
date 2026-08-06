"""YouTube Shorts client — channel Shorts tab, paginated through InnerTube.

Verified against live pages on 2026-08-06 from a datacenter IP:

* ``GET youtube.com/@handle/shorts`` returns the full first page with **no proxy and no TLS
  impersonation** — the data sits in the inline ``var ytInitialData = {...};`` blob, so there is
  nothing to render and no browser to pay for.
* Each Shorts entry is a ``shortsLockupViewModel``: 48 per page, carrying the videoId, title and
  a human view count ("101M views"). Google renamed this node from the older
  ``reelItemRenderer``; scrapers still looking for the old name find nothing on a page that looks
  perfectly fine, which is the quiet way this breaks.
* The other two listing types use DIFFERENT nodes, measured on the same day: a channel's
  ``/videos`` tab is 31x ``lockupViewModel`` (NOT ``videoRenderer`` — that is the old layout), and
  ``/results?search_query=`` is ``videoRenderer`` plus ``shortsLockupViewModel`` mixed together.
  One page shape does not imply the next, so each type declares its own node.
* Pagination is InnerTube: the page also ships ``INNERTUBE_API_KEY`` and
  ``INNERTUBE_CLIENT_VERSION``, and ytInitialData carries a ``continuationCommand`` token. POSTing
  that token to ``/youtubei/v1/browse`` returns the next 48 plus the next token. Verified: page 2
  returned 48 more and another token.

The client is deliberately paced. YouTube does not answer with a clean 429 the way Google Trends
does — it degrades, serving a consent/challenge page or an empty payload, which is far harder to
detect than an error code. Slow and boring is the cheaper failure mode.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.youtube.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MIN_GAP = 1.0
BACKOFF = (5.0, 15.0)
PAGE_SIZE_HINT = 48  # observed; used only for logging, never for control flow

log = logging.getLogger("youtube-shorts")


class ShortsError(RuntimeError):
    """Fetch or parse failed in a way retrying will not fix."""


class ShortsBlocked(ShortsError):
    """YouTube stopped answering with data — rotate the IP rather than waiting."""


class ShortsUnavailable(ShortsError):
    """A structure we depend on is gone (YouTube renamed or removed it)."""


def _find_all(node, key, out):
    """Collect every value stored under `key`, at any depth.

    YouTube moves nodes between wrappers constantly (tabs, shelves, view models), so walking for
    the leaf we want survives layout churn that a fixed path would not.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                out.append(v)
            else:
                _find_all(v, key, out)
    elif isinstance(node, list):
        for v in node:
            _find_all(v, key, out)
    return out


# Which node carries the rows, per listing type. Kept as data rather than branches so adding a
# type is one line and the parser stays one function.
LISTING_NODES = {
    "shorts": ("shortsLockupViewModel",),
    "videos": ("lockupViewModel",),
    # Search deliberately reads ONLY videoRenderer. The page also carries lockupViewModel cards,
    # but measured on a live search 3 of them had a contentId and no metadata at all — rows a
    # caller would be billed for and could not use.
    "search": ("videoRenderer",),
}

# Continuations are NOT all served by /browse. A search token POSTed to /browse is rejected with a
# bare HTTP 400 (measured), which reads like a dead token rather than the wrong endpoint — so the
# endpoint is declared per listing type instead of assumed.
LISTING_ENDPOINT = {"shorts": "browse", "videos": "browse", "search": "search"}


def channel_url(channel: str) -> str:
    """Accept a handle, a bare name, a /channel/UC… id or a full URL, return the Shorts tab."""
    c = (channel or "").strip()
    if not c:
        raise ShortsError("empty channel")
    if c.startswith("http://") or c.startswith("https://"):
        url = c.split("?")[0].rstrip("/")
        # Strip any tab already present so we never produce /videos/shorts.
        for tab in (
            "/shorts",
            "/videos",
            "/streams",
            "/featured",
            "/community",
            "/playlists",
        ):
            if url.endswith(tab):
                url = url[: -len(tab)]
        return url + "/shorts"
    if c.startswith("UC") and len(c) >= 20:
        return "%s/channel/%s/shorts" % (BASE, c)
    return "%s/@%s/shorts" % (BASE, c.lstrip("@"))


def listing_url(target: str, kind: str) -> str:
    """URL for a listing. `kind` is shorts | videos | search."""
    if kind == "search":
        return "%s/results?search_query=%s" % (BASE, urllib.parse.quote(target))
    url = channel_url(target)
    if kind == "videos":
        return url[: -len("/shorts")] + "/videos"
    return url


class ShortsClient:
    """One session against YouTube, optionally through one proxy.

    Bound to a single egress IP for its life: the consent cookies YouTube sets belong with the IP
    that got them. To rotate, build a new client (see `new_client` in main.py).
    """

    def __init__(
        self,
        min_gap: float = MIN_GAP,
        proxy_url: str | None = None,
        hl: str = "en",
        gl: str = "US",
    ):
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        self._op = urllib.request.build_opener(*handlers)
        self.proxy_url = proxy_url
        self.min_gap = float(min_gap)
        self.hl = hl
        self.gl = gl
        self._last = 0.0
        self.requests_made = 0
        self._api_key = None
        self._client_version = None

    # ---- transport ----------------------------------------------------------------------
    def _pace(self) -> None:
        wait = self.min_gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.3))
        self._last = time.monotonic()

    def _raw(self, url: str, data: bytes | None = None) -> str:
        headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "%s,en;q=0.9" % self.hl,
            "Accept-Encoding": "gzip",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        resp = self._op.open(
            urllib.request.Request(url, data=data, headers=headers), timeout=45
        )
        blob = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            blob = gzip.decompress(blob)
        return blob.decode("utf-8", "replace")

    def _fetch(self, url: str, data: bytes | None = None) -> str:
        for attempt, pause in enumerate((None,) + BACKOFF):
            if pause:
                log.warning(
                    "YouTube refused the request — waiting %.0fs then retrying", pause
                )
                time.sleep(pause)
            self._pace()
            try:
                self.requests_made += 1
                return self._raw(url, data)
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    continue
                if e.code == 404:
                    raise ShortsUnavailable("channel not found: %s" % url)
                raise ShortsError("HTTP %s for %s" % (e.code, url))
        raise ShortsBlocked("blocked after %d attempts: %s" % (len(BACKOFF) + 1, url))

    # ---- page 1 -------------------------------------------------------------------------
    def first_page(self, target: str, kind: str = "shorts") -> tuple[list, str | None, dict]:
        """(rows, continuation_token, info) for a channel tab or a search query."""
        url = listing_url(target, kind)
        html = self._fetch(url)

        m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
        if not m:
            # A consent wall or a challenge page still returns 200 with valid HTML, so an absent
            # blob means "we were served something else", not "the channel is empty".
            raise ShortsBlocked(
                "no ytInitialData on %s — served a consent/challenge page instead" % url
            )
        data = json.loads(m.group(1))

        # Cache the InnerTube credentials; they are per-page but stable for the session.
        k = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        v = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        if k:
            self._api_key = k.group(1)
        if v:
            self._client_version = v.group(1)

        shorts = self._parse_shorts(data, kind)
        if not shorts and self._looks_like_a_channel(data):
            # Distinguish "this channel posts no Shorts" (legitimate, empty) from "our parser is
            # blind" (our bug). Only the second should look like a failure.
            log.info("%s has no Shorts", url)
        return shorts, self._continuation(data), self._channel_info(data)

    @staticmethod
    def _looks_like_a_channel(data: dict) -> bool:
        return bool(
            _find_all(data, "c4TabbedHeaderRenderer", [])
            or _find_all(data, "pageHeaderRenderer", [])
        )

    # ---- pagination ---------------------------------------------------------------------
    def next_page(self, token: str, kind: str = "shorts") -> tuple[list, str | None]:
        if not (self._api_key and self._client_version):
            raise ShortsError(
                "call first_page() before paginating — no InnerTube credentials yet"
            )
        body = json.dumps(
            {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": self._client_version,
                        "hl": self.hl,
                        "gl": self.gl,
                    }
                },
                "continuation": token,
            }
        ).encode()
        raw = self._fetch(
            "%s/youtubei/v1/%s?key=%s&prettyPrint=false"
            % (BASE, LISTING_ENDPOINT.get(kind, "browse"), self._api_key),
            data=body,
        )
        data = json.loads(raw)
        return self._parse_shorts(data, kind), self._continuation(data)

    @staticmethod
    def _continuation(data: dict) -> str | None:
        for cmd in _find_all(data, "continuationCommand", []):
            tok = (cmd or {}).get("token")
            if tok:
                return tok
        return None

    # ---- parsing ------------------------------------------------------------------------
    @classmethod
    def _parse_shorts(cls, data: dict, kind: str = "shorts") -> list:
        """Rows for one listing type. Each node shape gets its own reader because YouTube gives
        them genuinely different fields — a search hit has a channel and a publish date that a
        channel listing does not, and pretending otherwise would silently drop them."""
        rows, seen = [], set()
        for node in LISTING_NODES.get(kind, LISTING_NODES["shorts"]):
            for vm in _find_all(data, node, []):
                row = (cls._one_short(vm) if node == "shortsLockupViewModel"
                       else cls._one_lockup(vm) if node == "lockupViewModel"
                       else cls._one_video_renderer(vm))
                vid = row.get("videoId")
                ctype = row.pop("contentType", None)
                # Drop what is not a usable video row. A listing mixes in playlist and channel
                # cards, and some cards carry an id with no metadata whatsoever — billing for a
                # row with no title is charging for nothing, so both are filtered here rather
                # than left for the caller to notice.
                if ctype and "VIDEO" not in str(ctype).upper():
                    continue
                if not row.get("title"):
                    continue
                # A listing repeats a video across shelves; dedupe so the caller is not charged
                # twice for the same row.
                if vid and vid not in seen:
                    seen.add(vid)
                    rows.append(row)
        return rows

    @staticmethod
    def _one_lockup(vm: dict) -> dict:
        """`lockupViewModel` — the current channel /videos and mixed-shelf shape."""
        vid = vm.get("contentId")
        meta = ((vm.get("metadata") or {}).get("lockupMetadataViewModel") or {})
        title = ((meta.get("title") or {}).get("content"))
        rows_meta = (((meta.get("metadata") or {}).get("contentMetadataViewModel") or {})
                     .get("metadataRows") or [])
        bits = []
        for r in rows_meta:
            for part in (r.get("metadataParts") or []):
                t = ((part.get("text") or {}).get("content"))
                if t:
                    bits.append(t)
        views = next((b for b in bits if "view" in b.lower()), None)
        published = next((b for b in bits if "ago" in b.lower()), None)
        thumbs = (((vm.get("contentImage") or {}).get("thumbnailViewModel") or {})
                  .get("image") or {}).get("sources") or []
        return {
            "videoId": vid,
            "contentType": vm.get("contentType"),
            "title": title,
            "url": ("https://www.youtube.com/watch?v=%s" % vid) if vid else None,
            "viewCountText": views,
            "viewCount": _parse_views(views),
            "publishedText": published,
            "thumbnail": (thumbs[-1].get("url") if thumbs else None),
            "metadataParts": bits or None,
        }

    @staticmethod
    def _one_video_renderer(vm: dict) -> dict:
        """`videoRenderer` — the search-results shape, which alone carries the channel name."""
        def txt(node):
            if not isinstance(node, dict):
                return None
            if node.get("simpleText"):
                return node["simpleText"]
            runs = node.get("runs") or []
            return "".join(r.get("text", "") for r in runs) or None
        vid = vm.get("videoId")
        thumbs = ((vm.get("thumbnail") or {}).get("thumbnails")) or []
        owner = (vm.get("ownerText") or vm.get("longBylineText") or {})
        return {
            "videoId": vid,
            "title": txt(vm.get("title")),
            "url": ("https://www.youtube.com/watch?v=%s" % vid) if vid else None,
            "viewCountText": txt(vm.get("viewCountText")),
            "viewCount": _parse_views(txt(vm.get("viewCountText"))),
            "publishedText": txt(vm.get("publishedTimeText")),
            "durationText": txt(vm.get("lengthText")),
            "channelName": txt(owner),
            "description": txt(vm.get("detailedMetadataSnippets", [{}])[0].get("snippetText"))
                           if vm.get("detailedMetadataSnippets") else None,
            "thumbnail": (thumbs[-1].get("url") if thumbs else None),
        }

    @staticmethod
    def _one_short(vm: dict) -> dict:
        overlay = vm.get("overlayMetadata") or {}
        title = (overlay.get("primaryText") or {}).get("content")
        views_text = (overlay.get("secondaryText") or {}).get("content")
        tap = (vm.get("onTap") or {}).get("innertubeCommand") or {}
        vid = (tap.get("reelWatchEndpoint") or {}).get("videoId")
        thumbs = ((vm.get("thumbnail") or {}).get("sources")) or []
        return {
            "videoId": vid,
            "title": title,
            "url": ("https://www.youtube.com/shorts/%s" % vid) if vid else None,
            "viewCountText": views_text,
            "viewCount": _parse_views(views_text),
            "thumbnail": (thumbs[-1].get("url") if thumbs else None),
            "accessibilityText": ((vm.get("accessibilityText")) or None),
        }

    @staticmethod
    def _channel_info(data: dict) -> dict:
        for h in _find_all(data, "pageHeaderRenderer", []):
            return {"channelName": h.get("pageTitle")}
        for h in _find_all(data, "c4TabbedHeaderRenderer", []):
            return {
                "channelName": h.get("title"),
                "subscriberCountText": (h.get("subscriberCountText") or {}).get(
                    "simpleText"
                ),
            }
        return {}


_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_views(text) -> int | None:
    """ "1.2B views" -> 1200000000. Returns None rather than 0 when unparseable: 0 would be a
    lie a caller could quietly aggregate, whereas None is visibly missing."""
    if not text:
        return None
    # "No views" is a real zero, not an unknown — YouTube's own wording for a video nobody has
    # watched yet. Left as None it would look like a parse failure.
    if "no view" in str(text).lower():
        return 0
    m = re.search(r"([\d.,]+)\s*([KMB])?", str(text).replace(",", ""))
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    return int(n * _SUFFIX.get((m.group(2) or "").upper(), 1))
