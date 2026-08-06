# google-data-mcp

An MCP server for four Google data surfaces that answer to plain HTTP — **no API key, no browser,
no scraping service**:

| Tool | What it returns |
|---|---|
| `google_trends_interest` | Interest over time for one keyword (0–100, weekly or hourly points) |
| `google_trends_compare` | Up to 5 keywords in ONE request, so the values *are* comparable |
| `google_trends_trending` | What is trending right now in a country |
| `youtube_listing` | A channel's Shorts, a channel's videos, or search results |
| `google_ads_advertisers` | Advertisers in the Ads Transparency Center, by name |
| `google_ads_creatives` | Every ad an advertiser runs — format, preview URL, first/last shown |
| `google_play_reviews` | App reviews — rating, text, date, version, developer reply |

## Read this before you install

**Everything leaves from your machine's IP, and Google's per-IP budget accumulates over hours.**
This is not a caveat buried at the bottom; it is the main thing that decides whether this server is
right for your job.

Measured: an unpaced burst was rate-limited at request 93, and after roughly 130 *paced* requests
spread over hours the same address was refused on the first request of a fresh session. While
writing this server, both the author's IPs — a home connection and a datacenter VPS — ended the day
refused by Google Trends.

So this server caches every result on disk (`~/.cache/google-data-mcp`) and paces its requests. That
is enough for **interactive use**: asking an agent a handful of questions, exploring a topic,
checking a competitor. It is **not** enough for bulk work, and no amount of local code can make one
IP behave like many.

If you need volume, the same clients run behind rotating proxies as Apify Actors:
[Google Trends](https://apify.com/leonguyen2808/google-trends-cached) ·
[YouTube](https://apify.com/leonguyen2808/youtube-shorts-scraper) ·
[Ads Transparency](https://apify.com/leonguyen2808/google-ads-transparency).

## What is deliberately missing

**YouTube transcripts.** They look available — the watch page still lists caption tracks — but
`api/timedtext` returns zero bytes and `/youtubei/v1/get_transcript` answers `Precondition check
failed` even when sent the page's own `INNERTUBE_CONTEXT` and `visitorData`, and the ANDROID and IOS
player clients are refused the same way. A transcript tool here would be a promise this server
cannot keep, so there isn't one.

## Install

```bash
pipx install git+https://github.com/ducnhd/google-data-mcp
# or: pip install git+https://github.com/ducnhd/google-data-mcp
```

Then register it with your MCP client. For Claude Code:

```bash
claude mcp add google-data -- google-data-mcp
```

Or by hand, in an MCP client config:

```json
{
  "mcpServers": {
    "google-data": {
      "command": "google-data-mcp"
    }
  }
}
```

## Notes on the data

* **Trends values are relative within a single request.** Two separate `google_trends_interest`
  calls are not comparable to each other; that is what `google_trends_compare` is for. Google caps a
  comparison at 5 terms and silently drops a 6th, so a 6th is refused rather than quietly ignored.
* **Play reviews differ per locale.** Each `hl`/`gl` pair returns a *different* set of reviews — six
  locales gave 120 unique reviews of the same app. Vary them to widen coverage rather than paging
  deeper in one language.
* **Reviewer identity is omitted by default.** The records carry an author name, avatar and Google
  account id; rating, text, date and version answer product questions without them. Set
  `include_author` if you genuinely need the name.
* **Zero ads is a real answer.** Google keeps advertisers whose ads have stopped running and still
  reports a count for them, so an empty `ads` list with a non-zero declared count is information,
  not a failure.
* `searchVolume` on trending terms is Google's own rounded bucket, not a precise count.

## Legal

These are public pages, but none of them has an official public API and each platform's Terms of
Service restrict automated access. You are responsible for how you use the output. No personal data
is collected by default.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m google_data_mcp        # speaks MCP over stdio
```

Each client is plain standard library and can be exercised on its own, which is the fastest way to
check whether Google changed something:

```bash
.venv/bin/python -c "from google_data_mcp.play import PlayClient; print(len(PlayClient().reviews('com.spotify.music', limit=40)))"
```
