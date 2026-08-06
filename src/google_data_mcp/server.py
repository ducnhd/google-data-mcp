"""MCP server exposing four Google data surfaces that answer to plain HTTP.

Scope is deliberate. Every surface here was verified reachable without a browser, a captcha solver
or a residential proxy; surfaces that need those are left out rather than shipped as tools that fail
in the field. In particular **YouTube transcripts are absent on purpose** — `api/timedtext` returns
zero bytes and `get_transcript` answers "Precondition check failed" even with the page's own context,
so a transcript tool would be a promise this server cannot keep.

The honest limitation to keep in mind: this runs on your machine, so everything leaves from one IP,
and Google's per-IP budget accumulates over hours. Results are cached on disk and requests are
paced, which is enough for interactive use. It is not enough for bulk work — see the README.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import cache
from .ads import AdsClient, advertiser_id
from .play import PlayClient
from .trends import TrendsClient
from .youtube import ShortsClient

server = MCPServer(
    name="google-data",
    title="Google data (Trends, YouTube, Ads Transparency, Play)",
    instructions=(
        "Read-only access to four Google surfaces: Google Trends, YouTube listings, the Ads "
        "Transparency Center and Google Play reviews. No API key is needed. Requests are paced and "
        "cached on disk; prefer one broad call over many narrow ones, because Google rate-limits "
        "per IP and this server has only the one it is running on."
    ),
)


def _cached(kind: str, ttl: float, fetch, **parts):
    k = cache.key(kind, **parts)
    hit = cache.get(k, ttl)
    if hit is not None:
        return {"fromCache": True, **hit}
    value = fetch()
    cache.put(k, value)
    return {"fromCache": False, **value}


# ---- Google Trends ---------------------------------------------------------------------------
@server.tool(
    description=(
        "Google Trends interest over time for ONE keyword. Values are 0-100 relative WITHIN this "
        "single request, so they are not comparable to another call's numbers — use "
        "google_trends_compare when you need to rank terms against each other."
    )
)
def google_trends_interest(
    keyword: Annotated[str, Field(description="A single search term.")],
    geo: Annotated[
        str,
        Field(
            description="Country or sub-region code (US, VN, US-CA). Empty = worldwide."
        ),
    ] = "",
    timeframe: Annotated[
        str,
        Field(
            description="Google's own range string: 'today 12-m', 'today 5-y', 'now 7-d', 'all', or '2025-01-01 2025-12-31'."
        ),
    ] = "today 12-m",
) -> dict:
    def fetch():
        c = TrendsClient()
        widgets = c.explore(keyword, geo, timeframe)
        w = widgets.get("TIMESERIES")
        if w is None:
            raise RuntimeError("Google returned no time series for this query")
        raw = c.widget_data(w)
        points = [
            {
                "date": p.get("formattedAxisTime") or p.get("formattedTime"),
                "value": (p.get("value") or [None])[0],
                "isPartial": bool(p.get("isPartial")),
            }
            for p in ((raw.get("default") or {}).get("timelineData") or [])
        ]
        return {
            "keyword": keyword,
            "geo": geo,
            "timeframe": timeframe,
            "points": points,
        }

    return _cached("interest", 6 * 3600, fetch, kw=keyword, geo=geo, tf=timeframe)


@server.tool(
    description=(
        "Compare up to 5 keywords in ONE Google Trends request, so their 0-100 values ARE "
        "normalised against each other and can be ranked. Google silently drops a 6th term, so 6+ "
        "is refused rather than answered with a term missing."
    )
)
def google_trends_compare(
    keywords: Annotated[list[str], Field(description="2 to 5 search terms.")],
    geo: Annotated[
        str, Field(description="Country or sub-region code. Empty = worldwide.")
    ] = "",
    timeframe: Annotated[
        str, Field(description="Google range string, e.g. 'today 12-m'.")
    ] = "today 12-m",
) -> dict:
    terms = [k.strip() for k in keywords if k and k.strip()]
    if not 2 <= len(terms) <= 5:
        raise ValueError(
            "give between 2 and 5 keywords — Google normalises at most 5 in one request and "
            "silently ignores the rest"
        )

    def fetch():
        c = TrendsClient()
        widgets = c.explore(terms, geo, timeframe)
        w = widgets.get("TIMESERIES")
        if w is None:
            raise RuntimeError("Google returned no time series for this comparison")
        raw = c.widget_data(w)
        points = []
        for p in (raw.get("default") or {}).get("timelineData") or []:
            vals = p.get("value") or []
            points.append(
                {
                    "date": p.get("formattedAxisTime") or p.get("formattedTime"),
                    "values": {
                        t: (vals[i] if i < len(vals) else None)
                        for i, t in enumerate(terms)
                    },
                }
            )
        return {"keywords": terms, "geo": geo, "timeframe": timeframe, "points": points}

    return _cached("compare", 6 * 3600, fetch, kws=sorted(terms), geo=geo, tf=timeframe)


@server.tool(
    description=(
        "What is trending on Google right now in one country. `searchVolume` is Google's own "
        "rounded bucket (100000, 1000000), not a precise count."
    )
)
def google_trends_trending(
    geo: Annotated[
        str, Field(description="Two-letter country code, e.g. US, VN, GB.")
    ] = "US",
    limit: Annotated[
        int, Field(description="How many terms to return.", ge=1, le=200)
    ] = 25,
) -> dict:
    def fetch():
        rows = TrendsClient().trending_now(geo)
        return {"geo": geo, "trends": rows}

    out = _cached("trending", 1800, fetch, geo=geo)
    return {**out, "trends": (out.get("trends") or [])[:limit]}


# ---- YouTube ---------------------------------------------------------------------------------
@server.tool(
    description=(
        "List videos from YouTube: a channel's Shorts tab, a channel's videos tab, or search "
        "results. Returns videoId, title, view count, and (for videos/search) a publish date. "
        "Transcripts are NOT available from this server — Google gates them behind a token this "
        "server cannot mint."
    )
)
def youtube_listing(
    target: Annotated[
        str,
        Field(
            description="A channel (@handle, UC… id or URL) for 'shorts'/'videos'; a search query for 'search'."
        ),
    ],
    kind: Annotated[
        Literal["shorts", "videos", "search"],
        Field(description="Which listing to read."),
    ] = "videos",
    limit: Annotated[
        int,
        Field(
            description="Maximum rows. Pagination stops early if the listing runs out.",
            ge=1,
            le=300,
        ),
    ] = 30,
) -> dict:
    def fetch():
        c = ShortsClient()
        rows, token, info = c.first_page(target, kind)
        while token and len(rows) < limit:
            more, token = c.next_page(token, kind)
            if not more:
                break
            rows.extend(more)
        return {
            "target": target,
            "listing": kind,
            "channelInfo": info,
            "items": rows[:limit],
        }

    out = _cached("yt", 3 * 3600, fetch, t=target, k=kind, n=limit)
    return {**out, "items": (out.get("items") or [])[:limit]}


# ---- Ads Transparency -------------------------------------------------------------------------
@server.tool(
    description=(
        "Find advertisers in Google's Ads Transparency Center by name. Returns each advertiser's "
        "id, country and Google's declared ad count — pass the id to google_ads_creatives."
    )
)
def google_ads_advertisers(
    keyword: Annotated[str, Field(description="Advertiser or brand name to search.")],
    limit: Annotated[
        int, Field(description="How many matches to return.", ge=1, le=20)
    ] = 10,
) -> dict:
    def fetch():
        return {
            "keyword": keyword,
            "advertisers": AdsClient().search_advertisers(keyword, limit),
        }

    return _cached("adv", 24 * 3600, fetch, kw=keyword.lower(), n=limit)


@server.tool(
    description=(
        "Every ad an advertiser is currently running, from Google's Ads Transparency Center: "
        "creative id, format, preview URL and first/last shown dates. An advertiser with zero ads "
        "is a real answer — Google keeps advertisers whose ads have stopped running."
    )
)
def google_ads_creatives(
    advertiser: Annotated[
        str,
        Field(
            description="An AR… advertiser id, or a Transparency Center URL containing one."
        ),
    ],
    limit: Annotated[
        int, Field(description="Maximum ads to return.", ge=1, le=300)
    ] = 40,
) -> dict:
    adv = advertiser_id(advertiser)
    if not adv:
        raise ValueError(
            "not an advertiser id: expected AR… or a Transparency Center URL. Use "
            "google_ads_advertisers to look one up by name."
        )

    def fetch():
        c = AdsClient()
        ads, token = [], None
        while True:
            page, token = c.creatives_page(adv, min(40, limit - len(ads)), token)
            ads.extend(page)
            if not token or not page or len(ads) >= limit:
                break
        return {"advertiserId": adv, "adCount": len(ads), "ads": ads[:limit]}

    out = _cached("ads", 12 * 3600, fetch, adv=adv, n=limit)
    return {**out, "ads": (out.get("ads") or [])[:limit]}


# ---- Google Play ------------------------------------------------------------------------------
@server.tool(
    description=(
        "Reviews for an Android app: rating, text, date, app version, thumbs-up and the "
        "developer's reply. Reviewer names and avatars are omitted unless you ask for them. Note "
        "each language/country returns a DIFFERENT set of reviews, so vary hl/gl to widen coverage."
    )
)
def google_play_reviews(
    app_id: Annotated[
        str, Field(description="Android package name, e.g. com.spotify.music.")
    ],
    limit: Annotated[int, Field(description="Maximum reviews.", ge=1, le=400)] = 60,
    sort: Annotated[
        Literal["relevant", "newest"], Field(description="Google's own ordering.")
    ] = "relevant",
    hl: Annotated[
        str,
        Field(
            description="Language code. Each language returns a different set of reviews."
        ),
    ] = "en",
    gl: Annotated[str, Field(description="Country code.")] = "US",
    include_author: Annotated[
        bool,
        Field(
            description="Include the reviewer's display name. Off by default: it is personal data and rarely needed for product or ASO analysis."
        ),
    ] = False,
) -> dict:
    def fetch():
        rows = PlayClient(hl=hl, gl=gl).reviews(
            app_id,
            limit=limit,
            sort=1 if sort == "relevant" else 2,
            include_author=include_author,
        )
        return {"appId": app_id, "hl": hl, "gl": gl, "sort": sort, "reviews": rows}

    out = _cached(
        "play",
        6 * 3600,
        fetch,
        app=app_id,
        n=limit,
        s=sort,
        hl=hl,
        gl=gl,
        a=include_author,
    )
    return {**out, "reviews": (out.get("reviews") or [])[:limit]}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
