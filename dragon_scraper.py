#!/usr/bin/env python3
"""
Dragon IPTV scraper
- Opens each live page in headless Chromium (Playwright)
- Captures the first real .m3u8 request the page makes
- Writes dragon.m3u8 (sorted A-Z by tvg-name) and dragon.xml.gz (XMLTV EPG)
"""
import asyncio
import gzip
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

from playwright.async_api import async_playwright

OUT_M3U = "dragon.m3u8"
OUTPUT_PLAYLISTS = {
    OUT_M3U: "generic",
    "dragon_vlc.m3u8": "vlc",
    "dragon_tivimate.m3u8": "tivimate",
    "dragon_jellyfin.m3u8": "jellyfin",
}
OUT_XML = "dragon.xml.gz"

CONCURRENCY = 4          # pages scraped in parallel
PAGE_TIMEOUT_MS = 45000  # page load timeout
STREAM_WAIT_SEC = 20     # how long to wait for an m3u8 after load
RETRIES = 2              # attempts per channel

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Requests containing any of these are ad/tracking manifests, not the channel
AD_BLOCKLIST = ("doubleclick", "googleads", "imasdk", "adsrvr", "/ads/", "adserver", "adsystem")

ENTERTAINMENT = "EN - Entertainment"
NEWS = "EN - News"
MUSIC = "Music"

CHANNELS = [
    # ---- EN - Entertainment ----
    ("Comet TV", "https://comettv.com/watch-live/", ENTERTAINMENT),
    ("Charge!", "https://watchcharge.com/watch-live/", ENTERTAINMENT),
    ("Roar", "https://watchroar.com/watch-live/", ENTERTAINMENT),
    ("The Nest", "https://watchthenest.com/watch-live/", ENTERTAINMENT),
    ("Tennis Channel", "https://www.tennischannel.com/live", ENTERTAINMENT),
    ("Circle Country", "https://www.circlecountry.com/", ENTERTAINMENT),
    ("QVC+", "https://plus.qvc.com/", ENTERTAINMENT),
    ("Shop LC", "https://www.shoplc.com/pages/live-tv", ENTERTAINMENT),
    ("JTV Live", "https://www.jtv.com/show/jtv-live", ENTERTAINMENT),
    ("Gems TV", "https://www.gems.com/", ENTERTAINMENT),
    ("Beach TV WPCT", "https://wpct.beachtv.tv/", ENTERTAINMENT),
    ("Beach TV WAWD", "https://wawd.beachtv.tv/", ENTERTAINMENT),
    ("Beach TV WGSC", "https://wgsc.beachtv.tv/", ENTERTAINMENT),
    ("Florida Keys TV WCAY", "https://wcay.floridakeystv.tv/", ENTERTAINMENT),
    ("Bally Sports", "https://www.ballysports.com", ENTERTAINMENT),
    # ---- EN - News ----
    ("CBS News 24/7", "https://www.cbsnews.com/live/", NEWS),
    ("CBS News New York", "https://www.cbsnews.com/newyork/live/", NEWS),
    ("CBS News Los Angeles", "https://www.cbsnews.com/losangeles/live/", NEWS),
    ("CBS News Chicago", "https://www.cbsnews.com/chicago/live/", NEWS),
    ("CBS News Texas", "https://www.cbsnews.com/texas/live/", NEWS),
    ("CBS News Philadelphia", "https://www.cbsnews.com/philadelphia/live/", NEWS),
    ("CBS News Bay Area", "https://www.cbsnews.com/sanfrancisco/live/", NEWS),
    ("CBS News Boston", "https://www.cbsnews.com/boston/live/", NEWS),
    ("CBS News Detroit", "https://www.cbsnews.com/detroit/live/", NEWS),
    ("CBS News Minnesota", "https://www.cbsnews.com/minnesota/live/", NEWS),
    ("CBS News Colorado", "https://www.cbsnews.com/colorado/live/", NEWS),
    ("CBS News Miami", "https://www.cbsnews.com/miami/live/", NEWS),
    ("CBS News Sacramento", "https://www.cbsnews.com/sacramento/live/", NEWS),
    ("CBS News Pittsburgh", "https://www.cbsnews.com/pittsburgh/live/", NEWS),
    ("CBS News Baltimore", "https://www.cbsnews.com/baltimore/live/", NEWS),
    ("ABC News Live", "https://abcnews.com/live/", NEWS),
    ("NBC New York", "https://www.nbcnews.com/new-york", NEWS),
    ("NBC Los Angeles", "https://www.nbcnews.com/los-angeles", NEWS),
    ("NBC Chicago", "https://www.nbcnews.com/chicago", NEWS),
    ("NBC Dallas-Fort Worth", "https://www.nbcnews.com/dallas-fort-worth", NEWS),
    ("NBC Philadelphia", "https://www.nbcnews.com/philadelphia", NEWS),
    ("NBC Washington", "https://www.nbcnews.com/washington", NEWS),
    ("NBC Boston", "https://www.nbcnews.com/boston", NEWS),
    ("NBC Bay Area", "https://www.nbcnews.com/bay-area", NEWS),
    ("NBC Miami", "https://www.nbcnews.com/miami", NEWS),
    ("NBC San Diego", "https://www.nbcnews.com/san-diego", NEWS),
    ("NBC Connecticut", "https://www.nbcnews.com/connecticut", NEWS),
    ("UN Web TV", "https://webtv.un.org/en", NEWS),
    ("Court TV", "https://www.courttv.com/title/court-tv-live-stream-web/", NEWS),
    # ---- Music ----
    ("Dance TV", "https://www.dancetv.net/", MUSIC),
    ("TCN Country", "https://watch.tcncountry.com/", MUSIC),
    ("ACN Live", "https://acn.tv/acn-live/", MUSIC),
]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".") + ".us"


def is_stream_candidate(url: str) -> bool:
    low = url.lower()
    return ".m3u8" in low and not any(bad in low for bad in AD_BLOCKLIST)


VIDEO_CODEC_RE = re.compile(r"avc1|hvc1|hev1|vp09|av01")
AUDIO_URL_RE = re.compile(r"(audio|_a\d|-a\d|/a/|aac|radio|mp4a)")
AUDIO_SEG_RE = re.compile(r"\.(aac|mp3|m4a|ac3|ec3)(\?|$)", re.I)


async def score_candidate(context, url, referrer):
    """Fetch the playlist and score it: video masters win, audio-only loses."""
    score = -50 if AUDIO_URL_RE.search(url.lower()) else 0
    try:
        resp = await context.request.get(
            url, headers={"Referer": referrer, "User-Agent": USER_AGENT}, timeout=10000
        )
        text = await resp.text()
    except Exception:
        return score - 10

    if "#EXT-X-STREAM-INF" in text:
        inf_lines = [l for l in text.splitlines() if l.startswith("#EXT-X-STREAM-INF")]
        has_video = any("RESOLUTION=" in l or VIDEO_CODEC_RE.search(l) for l in inf_lines)
        score += 100 if has_video else -100
    elif "#EXTINF" in text:
        segs = [l for l in text.splitlines() if l and not l.startswith("#")]
        if segs and all(AUDIO_SEG_RE.search(s) for s in segs):
            score -= 100  # media playlist made only of audio segments
        else:
            score += 10
    return score


async def pick_best(context, candidates, referrer):
    if not candidates:
        return None
    scored = []
    for url in candidates:
        scored.append((await score_candidate(context, url, referrer), url))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_url = scored[0]
    if best_score <= -100:
        print(f"       only audio-only playlists found, skipping: {best_url}")
        return None
    return best_url


async def try_start_playback(page):
    selectors = [
        "button[aria-label*='play' i]",
        ".vjs-big-play-button",
        ".jw-icon-display",
        ".plyr__control--overlaid",
        "[class*='play-button' i]",
        "video",
    ]
    for sel in selectors:
        try:
            await page.click(sel, timeout=1500, force=True)
            break
        except Exception:
            continue


async def scrape_one(browser, name, url, group):
    for attempt in range(1, RETRIES + 1):
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        page = await context.new_page()
        candidates = []

        # Speed: we only need the manifest request, not images/fonts/media
        async def route_handler(route):
            if route.request.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)
        page.on(
            "request",
            lambda req: candidates.append(req.url)
            if is_stream_candidate(req.url) and req.url not in candidates
            else None,
        )

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(2500)
            await try_start_playback(page)

            waited = 0.0
            while waited < STREAM_WAIT_SEC and not candidates:
                await asyncio.sleep(0.5)
                waited += 0.5
            if candidates:
                await asyncio.sleep(5)  # let the master / video playlists show up

            stream = await pick_best(context, candidates, url)

            # Fallback: m3u8 URL embedded in page source
            if not stream:
                html = await page.content()
                m = re.search(r"https?:[^\"'\s<>]+?\.m3u8[^\"'\s<>]*", html.replace("\\/", "/"))
                if m and is_stream_candidate(m.group(0)):
                    stream = m.group(0)

            logo = ""
            try:
                logo = await page.evaluate(
                    "() => (document.querySelector('meta[property=\"og:image\"]')||{}).content || ''"
                )
            except Exception:
                pass

            if stream:
                print(f"[OK]   {name}: {stream}")
                return {
                    "name": name,
                    "group": group,
                    "url": stream,
                    "logo": logo,
                    "referrer": url,
                    "tvg_id": slugify(name),
                }
            print(f"[MISS] {name} (attempt {attempt}/{RETRIES})")
        except Exception as exc:
            print(f"[ERR]  {name} (attempt {attempt}/{RETRIES}): {exc}")
        finally:
            await context.close()
    return None


async def scrape_all():
    sem = asyncio.Semaphore(CONCURRENCY)
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--no-sandbox"],
        )

        async def worker(ch):
            async with sem:
                return await scrape_one(browser, *ch)

        found = await asyncio.gather(*(worker(c) for c in CHANNELS))
        results = [r for r in found if r]
        await browser.close()
    return results


def build_playlist(entries, style):
    """
    style:
      generic   - plain M3U + #EXTVLCOPT headers (dragon.m3u8)
      vlc       - #EXTVLCOPT referrer / user-agent lines
      tivimate  - pipe headers: url|User-Agent=...&Referer=...
      jellyfin  - plain URLs only (Jellyfin ignores per-stream headers), + tvg-chno
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    header = "#EXTM3U"
    if repo:
        epg = f"https://raw.githubusercontent.com/{repo}/{branch}/{OUT_XML}"
        header += f' x-tvg-url="{epg}" url-tvg="{epg}"'
    lines = [header]

    for num, e in enumerate(entries, start=1):
        chno = f' tvg-chno="{num}"' if style == "jellyfin" else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{e["tvg_id"]}" tvg-name="{e["name"]}"{chno} '
            f'tvg-logo="{e["logo"]}" group-title="{e["group"]}",{e["name"]}'
        )
        if style in ("generic", "vlc"):
            lines.append(f'#EXTVLCOPT:http-referrer={e["referrer"]}')
            lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
            lines.append(e["url"])
        elif style == "tivimate":
            lines.append(f'{e["url"]}|User-Agent={USER_AGENT}&Referer={e["referrer"]}')
        else:  # jellyfin
            lines.append(e["url"])
    return "\n".join(lines) + "\n"


def write_m3u(entries):
    # Sort alphabetically by tvg-name (case-insensitive)
    entries = sorted(entries, key=lambda e: e["name"].casefold())
    for path, style in OUTPUT_PLAYLISTS.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_playlist(entries, style))
    return entries


def write_epg(entries):
    """Minimal XMLTV: channel list + 4-hour placeholder blocks for the next 24h."""
    fmt = "%Y%m%d%H%M%S +0000"
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start0 = now - timedelta(hours=now.hour % 4)

    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="dragon-scraper">']
    for e in entries:
        xml.append(f'  <channel id={quoteattr(e["tvg_id"])}>')
        xml.append(f'    <display-name>{escape(e["name"])}</display-name>')
        if e["logo"]:
            xml.append(f'    <icon src={quoteattr(e["logo"])} />')
        xml.append("  </channel>")
    for e in entries:
        for i in range(6):
            s = start0 + timedelta(hours=4 * i)
            t = s + timedelta(hours=4)
            xml.append(
                f'  <programme start="{s.strftime(fmt)}" stop="{t.strftime(fmt)}" '
                f'channel={quoteattr(e["tvg_id"])}>'
            )
            xml.append(f'    <title lang="en">{escape(e["name"])} Live</title>')
            xml.append(f'    <category lang="en">{escape(e["group"])}</category>')
            xml.append("  </programme>")
    xml.append("</tv>")

    with gzip.open(OUT_XML, "wt", encoding="utf-8") as f:
        f.write("\n".join(xml))


def main():
    entries = asyncio.run(scrape_all())
    if not entries:
        print("No streams found; leaving existing files untouched.")
        sys.exit(1)
    entries = write_m3u(entries)
    write_epg(entries)
    print(f"\nWrote {len(entries)}/{len(CHANNELS)} channels to {', '.join(OUTPUT_PLAYLISTS)} and {OUT_XML}")


if __name__ == "__main__":
    main()
 
