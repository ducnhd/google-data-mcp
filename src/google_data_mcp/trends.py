"""Google Trends HTTP client.

Every non-obvious choice here comes from probing the live endpoints on 2026-08-06 from a
datacenter IP. Recording the measurements is the point: the numbers are what make the
difference between an Actor that fails a third of its runs and one that does not.

What was measured
-----------------
* ``/trends/api/explore`` and ``/trends/api/widgetdata/*`` are ALIVE and need no proxy and no
  TLS impersonation — plain urllib with a browser User-Agent works. The only state required is
  the ``NID`` cookie handed out by ``GET /trends/``.
* ``/trends/api/dailytrends`` and ``/trends/api/realtimetrends`` are **404 — retired**. Tools
  still calling them fail every time, which is the likeliest reason the incumbent scrapers
  carry poor ratings. Trending-now moved to the WIZ ``batchexecute`` RPC below.
* **Rate limit, short window**: a burst with no pacing died at request **93 in 13.9 s**
  (~6.6 req/s) with HTTP 429. Still 429 twenty seconds later, recovered by sixty.
* **Rate limit, long window — the one that actually matters**: after roughly 130 paced requests
  spread over a few hours, the SAME IP began answering 429 to the very first ``/explore`` of a
  fresh session. So the per-IP budget is not a short leaky bucket you can wait out in a minute;
  it accumulates. **One IP therefore cannot serve production volume, no matter how politely it
  is paced.** That is why this client takes a proxy URL and why the cache is not a nicety —
  every cache hit is a request that never spends the IP budget.
* ``/widgetdata/relatedsearches`` (related topics/queries) exhausts that budget noticeably
  faster than ``multiline``: it was the first endpoint to fail while the others still worked.

Responses are JSON prefixed with ``)]}'`` (an anti-JSON-hijacking guard), so the body is
always sliced at the first brace/bracket before parsing.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://trends.google.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Measured ceiling was ~6.6 req/s; 1.2 s (0.83 req/s) ran clean. Keep the default under 1 req/s
# and let a caller widen it only if it has its own proxy pool.
MIN_GAP = 1.2
# Waiting out a 429 is the WRONG default on a paid platform. Measured on a real run: 60 s of the
# 88 s wall clock was backoff sleep, and compute is billed per second × memory, so more than half
# the run's cost bought nothing but waiting. When a fresh IP is one call away, SWITCHING is both
# faster and cheaper than sleeping — so this client gives up quickly and lets the caller rotate
# (see `new_client` in main.py). The one short wait absorbs a momentary blip without a rotation.
BACKOFF = (4.0,)

log = logging.getLogger("google-trends")

# The WIZ rpcid serving the "Trending now" page. This is the same batchexecute mechanism Google
# uses across its products; the id CAN rotate on a front-end deploy, which is the one failure
# this module cannot work around — it surfaces as TrendsUnavailable rather than silent garbage.
TRENDING_RPCID = "i0OFE"

# Widget ids returned by /explore, mapped to the endpoint that serves each one.
WIDGET_ENDPOINT = {
    "TIMESERIES": "multiline",
    "GEO_MAP": "comparedgeo",
    "RELATED_TOPICS": "relatedsearches",
    "RELATED_QUERIES": "relatedsearches",
}


class TrendsError(RuntimeError):
    """A request failed in a way retrying will not fix."""


class TrendsBlocked(TrendsError):
    """Google answered 429 and the backoff budget ran out."""


class TrendsUnavailable(TrendsError):
    """An endpoint or RPC id we depend on no longer exists (Google changed it)."""


def _strip_prefix(body: str) -> str:
    """Drop the ``)]}'`` guard and return the JSON payload."""
    for i, ch in enumerate(body):
        if ch in "[{":
            return body[i:]
    raise TrendsError("no JSON in response: %r" % body[:120])


class TrendsClient:
    """One Google Trends session, optionally through one proxy.

    A client is bound to a single egress IP for its whole life, because the NID cookie and the
    per-IP budget belong together — swapping the proxy underneath an existing cookie jar looks
    like a hijacked session. To rotate, build a NEW client with a new proxy URL (see
    ``rotate_every`` in main.py).
    """

    def __init__(self, hl: str = "en-US", tz: int = 0, min_gap: float = MIN_GAP,
                 proxy_url: str | None = None):
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        self._op = urllib.request.build_opener(*handlers)
        self.proxy_url = proxy_url
        self.hl = hl
        self.tz = tz
        self.min_gap = float(min_gap)
        self._last = 0.0
        self._bootstrapped = False
        self.requests_made = 0

    # ---- transport ----------------------------------------------------------------------
    def _pace(self) -> None:
        """Sleep so consecutive requests never come closer than min_gap.

        Jitter keeps a fleet of runs from lining up into a synchronised burst, which is how a
        per-IP limit gets hit even when each run looks polite on its own.
        """
        wait = self.min_gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        self._last = time.monotonic()

    def _raw(self, url: str, data: bytes | None = None, ctype: str | None = None,
             referer: str | None = None) -> str:
        headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
        }
        if ctype:
            headers["Content-Type"] = ctype
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, data=data, headers=headers)
        resp = self._op.open(req, timeout=45)
        blob = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            blob = gzip.decompress(blob)
        return blob.decode("utf-8", "replace")

    def _get(self, url: str, **kw) -> str:
        """One request with pacing, plus 429 backoff.

        404 is NOT retried: on this API it means the endpoint is gone (as happened to
        dailytrends/realtimetrends), and retrying a retired endpoint only burns the caller's
        compute before failing anyway.
        """
        self._bootstrap()
        for attempt, pause in enumerate((None,) + BACKOFF):
            if pause:
                time.sleep(pause)
            self._pace()
            try:
                self.requests_made += 1
                return self._raw(url, **kw)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Never silent: a run that appears to hang is indistinguishable from a
                    # broken one, and this is the single most common way this Actor slows down.
                    nxt = BACKOFF[attempt] if attempt < len(BACKOFF) else None
                    log.warning(
                        "rate limited by Google (429) on %s%s",
                        url.split("?")[0],
                        (" — waiting %.0fs then retrying" % nxt) if nxt else " — giving up on this request",
                    )
                    continue
                if e.code == 404:
                    raise TrendsUnavailable("404 %s" % url.split("?")[0])
                raise TrendsError("HTTP %s %s" % (e.code, url.split("?")[0]))
        raise TrendsBlocked("429 after %d attempts: %s" % (len(BACKOFF) + 1, url.split("?")[0]))

    def _bootstrap(self) -> None:
        """Fetch the NID cookie once. Without it /explore answers 429 immediately."""
        if self._bootstrapped:
            return
        self._bootstrapped = True   # set first: a failure here must not loop
        self._pace()
        try:
            self.requests_made += 1
            self._raw(BASE + "/trends/")
        except Exception:
            # Not fatal on its own — /explore may still work — so let the real call report.
            pass

    # ---- interest data -----------------------------------------------------------------
    def explore(self, keywords, geo: str = "", timeframe: str = "today 12-m",
                category: int = 0) -> dict:
        """Return {widget_id: widget} — each widget carries the token its data call needs.

        `keywords` may be a single string or a list of up to 5 terms. A LIST is not the same as
        looping: Google normalises the 0-100 scale **within one request**, so terms sent together
        are directly comparable while terms fetched separately are not. It is also cheaper —
        3 terms cost 1 explore + 1 widget call instead of 6 calls.

        With more than one term the widget set changes shape: `TIMESERIES` and `GEO_MAP` cover the
        whole comparison, and per-term widgets appear suffixed (`GEO_MAP_0`, `RELATED_QUERIES_1`,
        …). Callers must not assume the single-term ids exist.
        """
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords = [k for k in keywords if str(k).strip()]
        if not keywords:
            raise TrendsError("explore needs at least one keyword")
        if len(keywords) > 5:
            # Google's own UI caps a comparison at 5; a 6th is silently dropped, which would
            # hand the caller a result quietly missing a term they paid for.
            raise TrendsError("Google Trends compares at most 5 terms at once, got %d" % len(keywords))
        req = json.dumps(
            {
                "comparisonItem": [
                    {"keyword": k, "geo": geo, "time": timeframe} for k in keywords
                ],
                "category": int(category),
                "property": "",
            },
            separators=(",", ":"),
        )
        url = "%s/trends/api/explore?hl=%s&tz=%d&req=%s" % (
            BASE, self.hl, self.tz, urllib.parse.quote(req))
        payload = json.loads(_strip_prefix(self._get(url)))
        widgets = {w.get("id"): w for w in payload.get("widgets", [])}
        if not widgets:
            raise TrendsError("explore returned no widgets for %r" % (keywords,))
        return widgets

    def widget_data(self, widget: dict) -> dict:
        """Fetch one widget's data. The widget's own `request` object is echoed back verbatim —
        it encodes the resolution and comparison Google chose, so never rebuild it by hand."""
        wid = widget.get("id", "")
        endpoint = WIDGET_ENDPOINT.get(wid)
        if not endpoint:
            raise TrendsError("unsupported widget %r" % wid)
        url = "%s/trends/api/widgetdata/%s?hl=%s&tz=%d&req=%s&token=%s" % (
            BASE, endpoint, self.hl, self.tz,
            urllib.parse.quote(json.dumps(widget["request"], separators=(",", ":"))),
            urllib.parse.quote(widget["token"]),
        )
        return json.loads(_strip_prefix(self._get(url)))

    # ---- trending now ------------------------------------------------------------------
    def trending_now(self, geo: str = "US", hours: int = 48) -> list[dict]:
        """The "Trending now" feed, via the WIZ batchexecute RPC.

        This replaced /api/dailytrends and /api/realtimetrends, both of which now 404. The
        response is a JSON-in-JSON-in-JSON envelope: the outer line holds ["wrb.fr", rpcid,
        "<json string>"], and that string holds the rows.
        """
        inner = json.dumps([None, None, geo, 0, "en", int(hours), 1])
        body = urllib.parse.urlencode(
            {"f.req": json.dumps([[[TRENDING_RPCID, inner, None, "generic"]]])}
        ).encode()
        raw = self._get(
            "%s/_/TrendsUi/data/batchexecute?rpcids=%s" % (BASE, TRENDING_RPCID),
            data=body,
            ctype="application/x-www-form-urlencoded;charset=UTF-8",
            referer="%s/trending?geo=%s" % (BASE, geo),
        )
        envelope = json.loads(_strip_prefix(raw).splitlines()[0])
        for frame in envelope:
            if isinstance(frame, list) and frame and frame[0] == "wrb.fr":
                rows = json.loads(frame[2])[1] or []
                return [self._trend_row(r) for r in rows]
        raise TrendsUnavailable(
            "batchexecute returned no wrb.fr frame for rpcid %s — the RPC id likely rotated"
            % TRENDING_RPCID
        )

    @staticmethod
    def _trend_row(r: list) -> dict:
        """Positional row -> named fields. Indexes verified against live responses (772 rows for
        US, 202 for VN); a missing index degrades to None rather than raising, because Google
        adds columns without warning.

        The original row is deliberately NOT kept. A trending feed is ~200-800 rows and each raw
        row is a deep nested array; carrying it would multiply dataset storage and cache size for
        data nobody reads — and on Apify the caller pays for that storage.
        """
        def at(i):
            return r[i] if isinstance(r, list) and len(r) > i else None
        started = at(3)
        return {
            "term": at(0),
            "geo": at(2),
            "startedAt": (started[0] if isinstance(started, list) and started else None),
            "searchVolume": at(6),
        }
