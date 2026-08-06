"""Google Play app pages and reviews, plain HTTP.

Two things worth knowing, both measured on 2026-08-06:

* The **first page of reviews is already in the app page**, inside the `AF_initDataCallback` block
  keyed `ds:10`, together with the pagination token at `ds:10[1][1]`. So 20 reviews cost one
  ordinary page fetch and no RPC at all.
* Pagination is the WIZ RPC rpcid **`UsvDTd`** (not `oCPfdb`, which answers `[3]`
  INVALID_ARGUMENT for every payload — a wrong rpcid is rejected exactly like a malformed one).
  40 reviews per request, unlimited depth.

Reviewer identity is **omitted by default**. The records carry an author name, an avatar URL and a
Google account id; rating, text, date, version and the developer's reply are what a product or ASO
question actually needs, so the personal fields are opt-in rather than shipped by accident.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEC = json.JSONDecoder()


class PlayError(RuntimeError):
    """Fetch or parse failed."""


def _get(url: str, data: bytes | None = None, ctype: str | None = None) -> str:
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if ctype:
        headers["Content-Type"] = ctype
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=45
        )
        blob = resp.read()
    except urllib.error.HTTPError as exc:
        raise PlayError("HTTP %s for %s" % (exc.code, url)) from None
    except urllib.error.URLError as exc:
        raise PlayError("%s unreachable: %s" % (url, exc)) from None
    try:
        blob = gzip.decompress(blob)
    except Exception:
        pass
    return blob.decode("utf-8", "replace")


def _blocks(html: str) -> dict:
    """Every `AF_initDataCallback` payload in the page, keyed by its `ds:N` name."""
    out = {}
    for m in re.finditer(r"AF_initDataCallback\((\{.*?\})\);</script>", html, re.S):
        raw = m.group(1)
        key = re.search(r"key:\s*'([^']+)'", raw)
        start = raw.find("data:")
        if not key or start < 0:
            continue
        body = raw[start + len("data:") :].lstrip()
        try:
            out[key.group(1)] = DEC.raw_decode(body)[0]
        except ValueError:
            continue
    return out


def _frames(txt: str) -> list:
    """batchexecute response: `)]}'`, a BLANK LINE, then length-prefixed JSON frames.

    The declared length counts the trailing newline, so slicing by it breaks the JSON. Let the
    decoder find the end instead — this is the trap that makes hand-rolled parsers fail.
    """
    if txt.startswith(")]}'"):
        txt = txt[4:]
    out, i = [], 0
    while i < len(txt):
        while i < len(txt) and txt[i] in "\r\n \t":
            i += 1
        if i >= len(txt):
            break
        if txt[i] != "[":
            nl = txt.find("\n", i)
            if nl < 0:
                break
            i = nl + 1
            continue
        obj, end = DEC.raw_decode(txt, i)
        out.append(obj)
        i = end
    return out


def _row(rec: list, include_author: bool = False) -> dict:
    out = {
        "reviewId": rec[0],
        "rating": rec[2] if len(rec) > 2 else None,
        "text": rec[4] if len(rec) > 4 else None,
        "thumbsUp": rec[6] if len(rec) > 6 else None,
        "appVersion": rec[10] if len(rec) > 10 else None,
        "date": None,
        "developerReply": None,
    }
    try:
        out["date"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(rec[5][0])))
    except Exception:
        pass
    try:
        out["developerReply"] = rec[7][1]
    except Exception:
        pass
    if include_author:
        try:
            out["authorName"] = rec[1][0]
        except Exception:
            out["authorName"] = None
    return out


class PlayClient:
    def __init__(self, hl: str = "en", gl: str = "US", min_gap: float = 1.0):
        self.hl, self.gl = hl, gl
        self.min_gap = float(min_gap)
        self._sid = self._bl = None
        self._last = 0.0
        self.requests_made = 0

    def _pace(self) -> None:
        wait = self.min_gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def app_page(self, app_id: str) -> tuple[list, str | None]:
        """(embedded review records, pagination token). One request, no RPC."""
        self._pace()
        self.requests_made += 1
        html = _get(
            "https://play.google.com/store/apps/details?id=%s&hl=%s&gl=%s"
            % (urllib.parse.quote(app_id), self.hl, self.gl)
        )
        sid = re.search(r'"FdrFJe":"([^"]+)"', html)
        bl = re.search(r'"cfb2h":"([^"]+)"', html)
        if not (sid and bl):
            raise PlayError(
                "no build labels in the page for %s — served a consent or error page, not the app"
                % app_id
            )
        self._sid, self._bl = sid.group(1), bl.group(1)
        block = _blocks(html).get("ds:10") or []
        recs = block[0] if block else []
        token = None
        try:
            token = block[1][1]
        except Exception:
            pass
        return recs, token

    def reviews_page(
        self, app_id: str, token: str | None = None, sort: int = 1, count: int = 40
    ) -> tuple[list, str | None]:
        """One RPC page. `sort` 1 = most relevant, 2 = newest."""
        if not (self._sid and self._bl):
            raise PlayError(
                "call app_page() first — the build labels come from the page"
            )
        url = (
            "https://play.google.com/_/PlayStoreUi/data/batchexecute?rpcids=UsvDTd"
            "&source-path=/store/apps/details&f.sid=%s&bl=%s&hl=%s&gl=%s&_reqid=1&rt=c"
            % (self._sid, self._bl, self.hl, self.gl)
        )
        inner = [None, None, [2, sort, [count, None, token], None, []], [app_id, 7]]
        payload = json.dumps(
            [[["UsvDTd", json.dumps(inner, separators=(",", ":")), None, "generic"]]],
            separators=(",", ":"),
        )
        self._pace()
        self.requests_made += 1
        raw = _get(
            url,
            urllib.parse.urlencode({"f.req": payload}).encode(),
            "application/x-www-form-urlencoded;charset=UTF-8",
        )
        for frame in _frames(raw):
            for row in frame:
                if row and row[0] == "wrb.fr" and row[1] == "UsvDTd" and row[2]:
                    data = json.loads(row[2])
                    nxt = None
                    try:
                        nxt = data[1][1]
                    except Exception:
                        pass
                    return data[0] or [], nxt
        return [], None

    def reviews(
        self,
        app_id: str,
        limit: int = 60,
        sort: int = 1,
        include_author: bool = False,
    ) -> list[dict]:
        recs, token = self.app_page(app_id)
        seen = {r[0] for r in recs}
        while token and len(recs) < limit:
            page, token = self.reviews_page(app_id, token, sort)
            fresh = [r for r in page if r[0] not in seen]
            if not fresh:
                break
            seen.update(r[0] for r in fresh)
            recs.extend(fresh)
        return [_row(r, include_author) for r in recs[:limit]]
