"""Collect the most popular English GTA 6 videos on YouTube.

Two sources, both optional, combined into one list:

1. YouTube Data API search (needs a free API key in the YOUTUBE_API_KEY
   environment variable). Runs the searches in config.json that are due this
   hour (fresh-gameplay searches every hour, broader most-viewed searches
   every few hours to save quota), then looks up fresh view, like and comment counts for every video kept.
2. Channel RSS feeds listed in config.json (no key needed). Each feed gives a
   channel's 15 latest uploads with their view counts; only GTA 6 videos are
   kept.

Every video is then run through the gameplay screen (see gameplay_check)
and flagged, so the page can show recent, popular gameplay on its own.

Results are merged with the previous run's docs/videos.json, so the list
builds up over time and view counts are refreshed whenever a video is seen
again. Writes:
  docs/videos.json - every kept video with its stats, for the web page
  docs/feed.xml    - RSS feed of the past two weeks' videos

Uses only the Python standard library.
"""

import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
OUT_DIR = ROOT / "docs"
DATA_FILE = OUT_DIR / "videos.json"

API = "https://www.googleapis.com/youtube/v3/"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
GAMING = "20"  # YouTube's Gaming category
UA = "Mozilla/5.0 (compatible; gta6-video-feed/1.0; +https://github.com)"


class FetchError(Exception):
    pass


def get(url, timeout=20):
    """Fetch a URL and return its body. Errors never include the URL, so an
    API key in the query string can't leak into the public Actions log."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read())["error"]["message"]
        except Exception:
            pass
        raise FetchError(f"HTTP {exc.code} {detail}".strip()) from None
    except Exception as exc:  # timeouts, DNS, connection resets
        raise FetchError(type(exc).__name__) from None


def api(endpoint, key, **params):
    params["key"] = key
    return json.loads(get(API + endpoint + "?" + urllib.parse.urlencode(params)))


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(text):
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def duration_seconds(text):
    """ISO 8601 duration such as PT1H2M3S -> 3723."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def is_gta6(text, keywords):
    text = text.lower()
    return any(k in text for k in keywords)


# ---------------------------------------------------------------- API source

def search_due(search, full_run):
    """Searches run on their own schedule to save quota: a search with
    "every_hours": 6 runs only when the UTC hour is a multiple of 6.
    Manual runs (FULL_RUN=1) run every search."""
    every = search.get("every_hours", 1)
    return full_run or every <= 1 or now().hour % every == 0


def search_api(config, key, log, full_run=False):
    """Video IDs from the view-count-ordered searches listed in config.json."""
    ids = []
    for search in config["searches"]:
        if not search_due(search, full_run):
            continue
        params = {
            "part": "id",
            "type": "video",
            "q": search["query"],
            "order": "viewCount",
            "maxResults": min(50, config.get("results_per_search", 50)),
            "safeSearch": "moderate",
        }
        if search.get("hours"):
            since = now() - timedelta(hours=search["hours"])
            params["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        if search.get("duration"):
            params["videoDuration"] = search["duration"]  # short <4 min, medium 4-20, long >20
        if search.get("gaming_only"):
            params["videoCategoryId"] = GAMING
        if search.get("language"):
            params["relevanceLanguage"] = search["language"]
        try:
            data = api("search", key, **params)
        except FetchError as exc:
            log.append(f"Search '{search['label']}' failed: {exc}")
            continue
        found = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
        print(f"  {search['label']}: {len(found)} videos")
        ids.extend(found)
    return ids


def lookup_api(ids, key, log):
    """Full details for up to any number of IDs, 50 per request (1 quota unit each).
    Returns {id: video}. IDs missing from the answer were deleted or made private."""
    out = {}
    ids = list(dict.fromkeys(ids))
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        try:
            data = api("videos", key, part="snippet,statistics,contentDetails", id=",".join(batch))
        except FetchError as exc:
            log.append(f"Video lookup failed: {exc}")
            return None  # unknown state: don't treat videos as deleted
        for v in data.get("items", []):
            sn, st = v["snippet"], v.get("statistics", {})
            out[v["id"]] = {
                "id": v["id"],
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "channel_id": sn.get("channelId", ""),
                "published": sn.get("publishedAt"),
                "description": (sn.get("description") or "")[:300],
                "views": int(st["viewCount"]) if "viewCount" in st else None,
                "likes": int(st["likeCount"]) if "likeCount" in st else None,
                "comments": int(st["commentCount"]) if "commentCount" in st else None,
                "duration": duration_seconds(v.get("contentDetails", {}).get("duration")),
                "live": sn.get("liveBroadcastContent", "none"),
                "category": sn.get("categoryId"),
                "lang": (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").split("-")[0].lower() or None,
            }
    return out


# ---------------------------------------------------------------- RSS source

def fetch_channel(channel, keywords):
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + urllib.parse.quote(channel["id"])
    root = ET.fromstring(get(url))
    videos = []
    for e in root.findall("atom:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        title = e.findtext("atom:title", "", NS)
        group = e.find("media:group", NS)
        desc = group.findtext("media:description", "", NS) if group is not None else ""
        if not vid or not is_gta6(title + " " + desc, keywords):
            continue
        stats = e.find("media:group/media:community/media:statistics", NS)
        rating = e.find("media:group/media:community/media:starRating", NS)
        link = e.find("atom:link[@rel='alternate']", NS)
        videos.append({
            "id": vid,
            "title": title,
            "channel": e.findtext("atom:author/atom:name", channel["name"], NS),
            "channel_id": e.findtext("yt:channelId", channel["id"], NS),
            "published": e.findtext("atom:published", None, NS),
            "description": desc[:300],
            "views": int(stats.get("views")) if stats is not None and stats.get("views") else None,
            "likes": int(rating.get("count")) if rating is not None and rating.get("count") else None,
            "short": link is not None and "/shorts/" in link.get("href", ""),
        })
    return videos


# ---------------------------------------------------------------- gameplay screen

def plain_words(text):
    """Lowercase, strip accents and punctuation: "ANÁLISIS!" -> "analisis"."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def gameplay_check(v, screen):
    """Return None if the video passes the gameplay screen, else the reason it failed.

    Passing means: posted since launch and within max_age_hours, long enough
    not to be a Short or clip, in YouTube's Gaming category, enough views to
    count as popular, and no title words that mark trailers, reactions,
    leaks, comparisons and the like. Title words are only used to exclude,
    because many gameplay titles aren't in English.
    """
    published = parse_time(v.get("published"))
    if not published:
        return "no date"
    if screen.get("posted_after") and published < parse_time(screen["posted_after"]):
        return "before launch"
    if now() - published > timedelta(hours=screen.get("max_age_hours", 72)):
        return "too old"
    if v.get("live") == "upcoming":
        return "not started"
    if v.get("live") != "live":  # live streams have no length yet
        if v.get("duration") is None or v["duration"] < screen.get("min_minutes", 5) * 60:
            return "too short"
    if screen.get("gaming_category_only") and v.get("category") and v["category"] != GAMING:
        return "not Gaming category"
    if (v.get("views") or 0) < screen.get("min_views", 0):
        return "too few views"
    title = " " + plain_words(v.get("title", "")) + " "
    for word in screen.get("exclude_title_words", []):
        if " " + plain_words(word) + " " in title:
            return f"title has '{word}'"
    return None


# ---------------------------------------------------------------- merge & output

def merge(old, new, stamp):
    """Update old records with new data, keeping fields the new data lacks."""
    rec = dict(old or {})
    for k, v in new.items():
        if v is not None:
            rec[k] = v
    rec.setdefault("first_seen", stamp)
    rec["checked"] = stamp
    return rec


def prune(videos, config):
    recent_cut = now() - timedelta(days=config.get("keep_recent_days", 30))
    by_views = sorted(videos.values(), key=lambda v: v.get("views") or 0, reverse=True)
    top = {v["id"] for v in by_views[: config.get("keep_top_all_time", 150)]}
    return {
        vid: v for vid, v in videos.items()
        if vid in top or (parse_time(v.get("published")) or now()) >= recent_cut
    }


def write_rss(config, items, path):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = config["title"]
    ET.SubElement(ch, "link").text = config.get("link", "")
    ET.SubElement(ch, "description").text = config.get("description", "")
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now())
    cut = now() - timedelta(days=14)
    recent = [v for v in items if (parse_time(v.get("published")) or now()) >= cut]
    for v in sorted(recent, key=lambda v: v.get("published") or "", reverse=True)[:100]:
        url = f"https://www.youtube.com/watch?v={v['id']}"
        node = ET.SubElement(ch, "item")
        ET.SubElement(node, "title").text = v["title"]
        ET.SubElement(node, "link").text = url
        ET.SubElement(node, "guid", isPermaLink="true").text = url
        views = f"{v['views']:,} views · " if v.get("views") is not None else ""
        ET.SubElement(node, "description").text = f"{views}{v['channel']}"
        ET.SubElement(node, "author").text = v["channel"]
        if v.get("published"):
            ET.SubElement(node, "pubDate").text = format_datetime(parse_time(v["published"]))
    ET.indent(rss)
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def main():
    config = json.loads(CONFIG.read_text())
    keywords = [k.lower() for k in config.get("keywords", [])]
    official = {c["id"] for c in config.get("channels", []) if c.get("official")}
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    stamp = iso(now())
    log = []
    OUT_DIR.mkdir(exist_ok=True)

    previous = {}
    if DATA_FILE.exists():
        try:
            previous = {v["id"]: v for v in json.loads(DATA_FILE.read_text()).get("items", [])}
        except Exception:
            log.append("Previous videos.json was unreadable; starting fresh.")
    videos = dict(previous)
    print(f"Starting with {len(videos)} videos from the last run.")

    print("Channel feeds:")
    for channel in config.get("channels", []):
        try:
            found = fetch_channel(channel, keywords)
        except (FetchError, ET.ParseError) as exc:
            log.append(f"Channel {channel['name']} skipped: {exc}")
            continue
        print(f"  {channel['name']}: {len(found)} GTA 6 videos")
        for v in found:
            videos[v["id"]] = merge(videos.get(v["id"]), v, stamp)

    if key:
        print("YouTube search:")
        full_run = os.environ.get("FULL_RUN") == "1"
        ids = search_api(config, key, log, full_run)
        details = lookup_api(ids + list(videos), key, log)
        if details is not None:
            for vid, v in details.items():
                videos[vid] = merge(videos.get(vid), v, stamp)
            gone = [vid for vid in videos if vid not in details]
            for vid in gone:
                del videos[vid]  # deleted, private or taken down
            if gone:
                print(f"  Removed {len(gone)} videos that are no longer available.")
    else:
        print("No YOUTUBE_API_KEY set: using channel feeds only (see README to add one).")

    # Keep only the languages listed in config.json. Videos with no language
    # info (channel-feed-only runs) are kept.
    langs = set(config.get("languages") or [])
    if langs:
        dropped = [vid for vid, v in videos.items() if v.get("lang") and v["lang"] not in langs]
        for vid in dropped:
            del videos[vid]
        if dropped:
            print(f"Dropped {len(dropped)} videos not in {', '.join(sorted(langs))}.")

    screen = config.get("gameplay_screen", {})
    for v in videos.values():
        v["official"] = v.get("channel_id") in official
        if v.get("live", "none") != "none":
            v["short"] = False  # live and upcoming streams report no length
        elif v.get("duration"):
            v["short"] = v["duration"] <= 60 or v.get("short", False)
        v["gameplay"] = gameplay_check(v, screen) is None
    print(f"Gameplay screen: {sum(v['gameplay'] for v in videos.values())} videos passed.")

    videos = prune(videos, config)
    items = sorted(videos.values(), key=lambda v: v.get("views") or 0, reverse=True)

    DATA_FILE.write_text(json.dumps({
        "updated": stamp,
        "source": "youtube-api+rss" if key else "rss",
        "count": len(items),
        "items": items,
    }, indent=1, ensure_ascii=False))
    write_rss(config, items, OUT_DIR / "feed.xml")
    print(f"Wrote {len(items)} videos to docs/videos.json and docs/feed.xml")

    if log:
        print("Problems:")
        for line in log:
            print("  - " + line)
    if not items:
        sys.exit("No videos found. Check the Actions log above and the channel IDs in config.json.")


if __name__ == "__main__":
    main()
