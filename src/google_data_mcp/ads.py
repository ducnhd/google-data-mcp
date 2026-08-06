"""Google Ads Transparency Center client — the `anji` RPC surface, plain HTTP.

Verified against the live endpoints on 2026-08-06 from a datacenter IP, no browser and no proxy.

What this surface is, and what it is not:

* It is **not** WIZ ``batchexecute``. There is no rpcid, no ``f.sid`` and no ``bl``. The methods are
  addressed by name: ``POST /anji/_/rpc/SearchService/<Method>``.
* Only **form-encoded ``f.req=<json>``** is accepted. A ``application/json+protobuf`` body is
  refused with a converter error that names the request class
  (``…reporting.SearchCreativesRequest``) — which is, in fact, the only informative error this
  surface produces.
* **A wrong field shape returns HTTP 200 and an empty ``{}``.** No error, no hint. That is why the
  field numbering below is stated as measured fact rather than derived at runtime: there is no
  signal to derive it from, and a caller must never be told "this advertiser has no ads" when the
  truth is "we sent the wrong request".

The page itself is useless for discovery — 2.5 MB of generic framework with no app strings, and a
full browser TLS fingerprint returns the same shell byte for byte.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BASE = "https://adstransparency.google.com/anji/_/rpc/%s"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MIN_GAP = 1.1
BACKOFF = (
    4.0,
)  # short: on a billed platform, rotating the IP beats waiting for a budget to reset

log = logging.getLogger("ads-transparency")


class AdsError(RuntimeError):
    """The request failed in a way retrying will not fix."""


class AdsBlocked(AdsError):
    """Rate-limited — rotate the egress IP rather than waiting it out."""


def _decode(blob: bytes) -> str:
    """Error bodies come back gzipped even when the success path does not, and reading them raw
    turns a perfectly clear message into mojibake — which is exactly how these errors got missed
    the first time round."""
    for attempt in (gzip.decompress, lambda b: zlib.decompress(b, -15), lambda b: b):
        try:
            return attempt(blob).decode("utf-8", "replace")
        except Exception:
            continue
    return blob.decode("utf-8", "replace")


def advertiser_id(text: str) -> str | None:
    """Pull an `AR…` id out of an id, a Transparency Center URL, or return None."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("AR") and t[2:].isdigit():
        return t
    for part in t.replace("?", "/").replace("&", "/").split("/"):
        if part.startswith("AR") and part[2:].isdigit():
            return part
    return None


class AdsClient:
    """One session, bound to one egress IP for its life. To rotate, build a new client."""

    def __init__(self, min_gap: float = MIN_GAP, proxy_url: str | None = None):
        handlers = []
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        self._op = urllib.request.build_opener(*handlers)
        self.proxy_url = proxy_url
        self.min_gap = float(min_gap)
        self._last = 0.0
        self.requests_made = 0

    def _pace(self) -> None:
        wait = self.min_gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.3))
        self._last = time.monotonic()

    def _rpc(self, method: str, payload: dict) -> dict:
        body = urllib.parse.urlencode(
            {"f.req": json.dumps(payload, separators=(",", ":"))}
        ).encode()
        req = urllib.request.Request(
            BASE % method,
            data=body,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept-Encoding": "gzip",
                "Referer": "https://adstransparency.google.com/",
                "X-Same-Domain": "1",
            },
        )
        for pause in (None,) + BACKOFF:
            if pause:
                log.warning(
                    "rate-limited by Google — waiting %.0fs then retrying once", pause
                )
                time.sleep(pause)
            self._pace()
            self.requests_made += 1
            try:
                raw = _decode(self._op.open(req, timeout=45).read())
            except urllib.error.HTTPError as exc:
                text = _decode(exc.read())
                if exc.code == 429:
                    continue
                # 400 here means the payload no longer matches the server's request class, i.e.
                # Google changed the schema. Surface the server's own words; they name the class.
                raise AdsError(
                    "HTTP %s from %s: %s" % (exc.code, method, text[:300])
                ) from None
            except urllib.error.URLError as exc:
                raise AdsError("%s unreachable: %s" % (method, exc)) from None
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise AdsError(
                    "%s returned non-JSON: %s" % (method, raw[:200])
                ) from None
        raise AdsBlocked("rate-limited by Google on %s" % method)

    # ---- advertisers -------------------------------------------------------------------
    def search_advertisers(self, keyword: str, limit: int = 10) -> list[dict]:
        """Advertisers whose name matches `keyword`, with their id, country and ad count."""
        data = self._rpc(
            "SearchService/SearchSuggestions", {"1": keyword, "2": limit, "3": limit}
        )
        rows = []
        for raw in data.get("1") or []:
            # Rows arrive wrapped one level deep.
            r = raw.get("1", raw) if isinstance(raw, dict) else {}
            count = ((r.get("4") or {}).get("2") or {}).get("1")
            rows.append(
                {
                    "advertiserName": r.get("1"),
                    "advertiserId": r.get("2"),
                    "advertiserCountry": r.get("3"),
                    # Google's own count for the advertiser. Reported separately from the number of
                    # creatives actually returned, because the two genuinely disagree: an
                    # advertiser whose ads have stopped running still declares a count.
                    "declaredAdCount": int(count)
                    if str(count or "").isdigit()
                    else None,
                    "advertiserUrl": (
                        "https://adstransparency.google.com/advertiser/%s" % r.get("2")
                        if r.get("2")
                        else None
                    ),
                }
            )
        return [r for r in rows if r["advertiserId"]]

    # ---- creatives ---------------------------------------------------------------------
    def creatives_page(
        self,
        advertiser: str,
        count: int = 40,
        token: str | None = None,
        region: int | None = None,
        domain: str = "",
    ) -> tuple[list[dict], str | None]:
        """One page of ads. `region` is Google's own enum and is left out by default — a wrong
        value returns zero rows with no error, which is indistinguishable from an advertiser that
        simply has no ads."""
        payload: dict = {
            "2": max(1, min(int(count), 100)),
            "3": {"12": {"1": domain, "2": True}, "13": {"1": [advertiser]}},
            "7": {"1": 1},
        }
        if region is not None:
            payload["3"]["8"] = [int(region)]
        if token:
            payload["4"] = token
        data = self._rpc("SearchService/SearchCreatives", payload)
        rows = [self._creative(c) for c in (data.get("1") or [])]
        return [r for r in rows if r.get("creativeId")], data.get("2")

    @staticmethod
    def _ts(node) -> str | None:
        """{1: unixSeconds, 2: nanos} -> ISO 8601. The seconds arrive as a STRING."""
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(node["1"])))
        except Exception:
            return None

    @staticmethod
    def _creative(c: dict) -> dict:
        """One creative record, as measured on live data.

        `4` is a format code and `3` is the content, which comes in two observed flavours: `3.1.4`
        is a rendered-preview URL, `3.3.2` is an HTML `<img>` snippet. The format is reported from
        the content actually present rather than from the code, because the code's full enum is not
        known and inventing labels for unseen values would be a guess dressed up as data. The raw
        code is passed through as `formatCode` so a caller can group by it regardless.
        """
        adv, cid = c.get("1"), c.get("2")
        content = c.get("3") or {}
        preview = image_html = fmt = None
        if isinstance(content.get("1"), dict):
            fmt, preview = "rendered_preview", content["1"].get("4")
        elif isinstance(content.get("3"), dict):
            fmt = "image"
            image_html = content["3"].get("2")
            if image_html:
                marker = 'src="'
                k = image_html.find(marker)
                if k >= 0:
                    preview = image_html[k + len(marker):].split('"', 1)[0]
        return {
            "advertiserId": adv,
            "advertiserName": c.get("12"),
            "creativeId": cid,
            "format": fmt,
            "formatCode": c.get("4"),
            "previewUrl": preview,
            "imageHtml": image_html,
            "creativeUrl": (
                "https://adstransparency.google.com/advertiser/%s/creative/%s" % (adv, cid)
                if adv and cid
                else None
            ),
            "firstShownAt": AdsClient._ts(c.get("6")),
            "lastShownAt": AdsClient._ts(c.get("7")),
        }
