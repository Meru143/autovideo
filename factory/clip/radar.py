"""Keyless live-stream and trending-video radar for clipping channels.

Sources:
  - Twitch live streams: public web GraphQL (no API key or account required),
    upgrading to Helix automatically if TWITCH_CLIENT_ID + TWITCH_TOKEN are set.
  - YouTube live streams: yt-dlp flat search + concurrent metadata probes.
  - YouTube trending videos: recent uploads ranked by views per hour.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

_Q_GAME = (
    "query($name:String!,$n:Int!){ game(name:$name){ displayName streams(first:$n){ edges{ node{ "
    "title viewersCount createdAt previewImageURL(width:640,height:360) broadcaster{ login displayName } "
    "} } } } }"
)
_Q_SEARCH = (
    "query($q:String!){ searchCategories(userQuery:$q,first:5){ edges{ node{ ... on Game{ name displayName } } } } }"
)

_YT_LIVE_SP = "&sp=EgJAAQ%253D%253D"
_YT_UPLOAD_DATE_SP = "&sp=CAI%253D"


def _ytdlp_base() -> list[str]:
    yt_dlp = shutil.which("yt-dlp")
    if yt_dlp:
        exe = [yt_dlp]
    elif importlib.util.find_spec("yt_dlp"):
        exe = [sys.executable, "-m", "yt_dlp"]
    else:
        raise RuntimeError("yt-dlp not found on PATH or in current Python environment")
    base = [*exe, "--no-warnings", "--no-progress"]
    if sys.platform == "win32":
        base += ["--compat-options", "no-certifi"]
    return base


def _ytdlp_json(url: str, extra: tuple[str, ...] = (), *, timeout: int = 60) -> dict[str, Any]:
    r = subprocess.run(
        [*_ytdlp_base(), "-J", *extra, url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"yt-dlp failed ({r.returncode}): {(r.stderr or r.stdout)[-300:]}")
    return json.loads(r.stdout)


def _parse_iso_utc(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None


def fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    sec = int(seconds)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def _twitch_gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    import requests

    r = requests.post(
        TWITCH_GQL_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Client-Id": TWITCH_WEB_CLIENT_ID},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()



_Q_ACTIVE_VOD = (
    "query($login:String!){ user(login:$login){ displayName login "
    "stream{ id title createdAt } "
    "videos(first:1, type:ARCHIVE){ edges{ node{ id title lengthSeconds createdAt status } } } "
    "} }"
)


def twitch_active_vod(login: str) -> dict[str, Any] | None:
    """Resolve a live Twitch streamer's in-progress archive VOD URL via keyless GQL."""
    clean = login.strip().lower().lstrip("#")
    if not clean:
        return None
    try:
        data = _twitch_gql(_Q_ACTIVE_VOD, {"login": clean}).get("data") or {}
    except Exception:
        return None
    user = data.get("user") or {}
    stream = user.get("stream")
    edges = ((user.get("videos") or {}).get("edges") or [])
    if not stream or not edges:
        return None
    node = edges[0].get("node") or {}
    vid = node.get("id")
    if not vid:
        return None
    start_ts = _parse_iso_utc(stream.get("createdAt") or node.get("createdAt"))
    elapsed_s = max(60.0, time.time() - start_ts) if start_ts else float(node.get("lengthSeconds") or 0.0)
    display = str(user.get("displayName") or clean).strip()
    handle = "@" + str(user.get("login") or clean).strip().lstrip("@")
    return {
        "vod_url": f"https://www.twitch.tv/videos/{vid}",
        "title": stream.get("title") or node.get("title") or clean,
        "creator": display,
        "creator_handle": handle,
        "duration": float(node.get("lengthSeconds") or elapsed_s),
        "elapsed_s": elapsed_s,
    }


def twitch_top_streams(game: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return live Twitch streams for a game category sorted by viewers desc.

    Uses Helix if TWITCH_CLIENT_ID and TWITCH_TOKEN are set; otherwise uses Twitch's
    keyless public web GraphQL endpoint.
    """
    cid, tok = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_TOKEN")
    now = time.time()
    if cid and tok:
        import requests

        headers = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
        g = requests.get("https://api.twitch.tv/helix/games", params={"name": game}, headers=headers, timeout=15).json()
        data = g.get("data") or []
        if data:
            gid = data[0]["id"]
            s = requests.get(
                "https://api.twitch.tv/helix/streams",
                params={"game_id": gid, "first": min(100, max(1, int(limit)))},
                headers=headers,
                timeout=15,
            ).json()
            rows = []
            for x in s.get("data") or []:
                st = _parse_iso_utc(x.get("started_at"))
                rows.append({
                    "platform": "Twitch",
                    "channel": x.get("user_login") or x.get("user_name") or "",
                    "name": x.get("user_name") or x.get("user_login") or "",
                    "title": x.get("title") or "",
                    "viewers": int(x.get("viewer_count") or 0),
                    "live_for": fmt_duration(now - st) if st else "",
                    "url": f"https://twitch.tv/{x.get('user_login', '')}",
                })
            return sorted(rows, key=lambda r: r["viewers"], reverse=True)[:limit]

    names = [game]
    try:
        d = _twitch_gql(_Q_SEARCH, {"q": game})
        for e in ((d.get("data") or {}).get("searchCategories") or {}).get("edges") or []:
            nm = (e.get("node") or {}).get("name")
            if nm and nm not in names:
                names.append(nm)
    except Exception:
        pass

    for nm in names:
        try:
            g = ((_twitch_gql(_Q_GAME, {"name": nm, "n": int(limit)}).get("data") or {}).get("game"))
        except Exception:
            continue
        if not g:
            continue
        rows = []
        for e in ((g.get("streams") or {}).get("edges") or []):
            n = e.get("node") or {}
            b = n.get("broadcaster") or {}
            login = b.get("login")
            if not login:
                continue
            st = _parse_iso_utc(n.get("createdAt"))
            rows.append({
                "platform": "Twitch",
                "channel": login,
                "name": b.get("displayName") or login,
                "title": n.get("title") or "",
                "viewers": int(n.get("viewersCount") or 0),
                "live_for": fmt_duration(now - st) if st else "",
                "url": f"https://twitch.tv/{login}",
            })
        if rows:
            return sorted(rows, key=lambda r: r["viewers"], reverse=True)[:limit]
    return []


def youtube_channel_live(handle: str, *, timeout: int = 20) -> dict[str, Any] | None:
    """Directly probe https://www.youtube.com/@<handle>/live (permanent, title-independent).

    Returns a normalized live-stream dict if the channel is currently live, else None.
    """
    clean = str(handle or "").strip().lstrip("@")
    if not clean:
        return None
    live_url = f"https://www.youtube.com/@{clean}/live"
    try:
        info = _ytdlp_json(live_url, ("--skip-download",), timeout=timeout)
    except Exception:
        return None
    if not info or not info.get("is_live"):
        return None
    now = time.time()
    start = info.get("release_timestamp") or info.get("timestamp")
    return {
        "platform": "YouTube",
        "channel": info.get("channel") or info.get("uploader") or clean,
        "name": info.get("channel") or info.get("uploader") or clean,
        "handle": clean,
        "title": info.get("title") or "",
        "description": str(info.get("description") or "")[:400],
        "tags": list(info.get("tags") or []),
        "viewers": int(info.get("concurrent_view_count") or 0),
        "live_for": fmt_duration(now - float(start)) if start else "",
        "url": info.get("webpage_url") or live_url,
    }


def youtube_live_streams(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return live YouTube streams matching query ranked by concurrent viewers."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}{_YT_LIVE_SP}"
    try:
        flat = _ytdlp_json(url, ("--flat-playlist", "--playlist-end", str(int(limit))))
    except Exception:
        return []
    urls = []
    for e in flat.get("entries") or []:
        if e and e.get("id"):
            urls.append(e.get("url") or f"https://www.youtube.com/watch?v={e['id']}")

    def _one(u: str) -> dict[str, Any] | None:
        try:
            return _ytdlp_json(u, ("--skip-download",), timeout=30)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        infos = [i for i in ex.map(_one, urls) if i and i.get("is_live")]

    now = time.time()
    rows = []
    for i in infos:
        start = i.get("release_timestamp") or i.get("timestamp")
        rows.append({
            "platform": "YouTube",
            "channel": i.get("channel") or i.get("uploader") or "",
            "name": i.get("channel") or i.get("uploader") or "",
            "title": i.get("title") or "",
            "viewers": int(i.get("concurrent_view_count") or 0),
            "live_for": fmt_duration(now - float(start)) if start else "",
            "url": i.get("webpage_url") or "",
        })
    return sorted(rows, key=lambda r: r["viewers"], reverse=True)[:limit]


def youtube_trending_videos(query: str, *, hours: float = 24.0, limit: int = 25) -> list[dict[str, Any]]:
    """Return recent non-live YouTube videos matching query ranked by views/hour."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}{_YT_UPLOAD_DATE_SP}"
    try:
        flat = _ytdlp_json(url, ("--flat-playlist", "--playlist-end", str(int(limit))))
    except Exception:
        return []
    urls = [
        e["url"] for e in (flat.get("entries") or [])
        if e and e.get("url") and "/shorts/" not in str(e["url"])
    ]

    def _one(u: str) -> dict[str, Any] | None:
        try:
            return _ytdlp_json(u, ("--skip-download",), timeout=30)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        infos = [i for i in ex.map(_one, urls) if i]

    now = time.time()
    rows = []
    for i in infos:
        ts = i.get("timestamp") or i.get("release_timestamp")
        if not ts or i.get("is_live"):
            continue
        age_h = max(0.25, (now - float(ts)) / 3600.0)
        if age_h > float(hours):
            continue
        views = int(i.get("view_count") or 0)
        rows.append({
            "views_per_hour": round(views / age_h, 1),
            "views": views,
            "age_h": round(age_h, 1),
            "duration_min": round(float(i.get("duration") or 0) / 60.0, 1),
            "channel": i.get("channel") or i.get("uploader") or "",
            "title": i.get("title") or "",
            "url": i.get("webpage_url") or "",
            "has_heatmap": bool(i.get("heatmap")),
        })
    return sorted(rows, key=lambda r: r["views_per_hour"], reverse=True)


def scan_radar(
    *,
    query: str,
    mode: str = "all",
    limit: int = 15,
    hours: float = 24.0,
) -> dict[str, Any]:
    """Run stream and/or trend discovery and return structured results."""
    out: dict[str, Any] = {
        "query": query,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if mode in ("streams", "all"):
        tw = twitch_top_streams(query, limit=limit)
        yt = youtube_live_streams(query, limit=limit)
        out["streams"] = sorted([*tw, *yt], key=lambda r: int(r.get("viewers") or 0), reverse=True)
    if mode in ("trends", "all"):
        out["trends"] = youtube_trending_videos(query, hours=hours, limit=limit)
    return out


def format_radar_table(data: dict[str, Any]) -> str:
    """Format scan_radar() output as a concise terminal table."""
    lines = [f"Radar: {data.get('query')} ({data.get('checked_at')})"]
    if "streams" in data:
        lines.append("\nLIVE STREAMS (Twitch + YouTube):")
        streams = data["streams"]
        if not streams:
            lines.append("  (none found)")
        else:
            for idx, r in enumerate(streams, 1):
                lines.append(
                    f"  {idx:2d}. [{r['platform']:7s}] {int(r.get('viewers') or 0):>7,} viewers"
                    f"  {r.get('live_for', ''):>6s}  {str(r.get('channel', ''))[:20]:20s}"
                    f"  {str(r.get('title', ''))[:44]:44s}  {r.get('url', '')}"
                )
    if "trends" in data:
        lines.append("\nTRENDING VIDEOS (YouTube, views/hr):")
        trends = data["trends"]
        if not trends:
            lines.append("  (none found)")
        else:
            for idx, r in enumerate(trends, 1):
                hm = " [heatmap]" if r.get("has_heatmap") else ""
                lines.append(
                    f"  {idx:2d}. {int(r['views_per_hour']):>8,}/h  ({int(r['views']):>9,} views, {r['age_h']:4.1f}h)"
                    f"  {str(r.get('channel', ''))[:18]:18s}  {str(r.get('title', ''))[:40]:40s}{hm}  {r.get('url', '')}"
                )
    return "\n".join(lines)


_TWITCH_VOD_CHAT_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
_CHAT_HYPE_RE = None


def _score_chat_messages(
    messages: list[tuple[float, str]],
    *,
    window_s: int = 10,
    top_k: int = 15,
    min_gap_s: float = 45.0,
) -> list[dict[str, Any]]:
    """Compute top chat-explosion peaks from (offset_s, text) messages using z-scored 10s buckets."""
    import math
    import re
    from collections import Counter

    if len(messages) < 30:
        return []
    hype_re = re.compile(r"(?i:LMAO|KEKW|OMEGALUL|LOL|CLIP|HOLY|NO WAY)|\bW+\b|[💀😂🔥]")
    noise_re = re.compile(r"(?i)giveaway|\braid(ing|ed)?\b|subscribe|discord\.gg|https?://|promo code")
    stop = frozenset("the a an and or to of is it in i you that this on for with was are be so but not my me we he she they what just like at as".split())
    word_re = re.compile(r"[\w']{2,}|[💀😂🔥]")

    raw_b: Counter[int] = Counter()
    wgt_b: Counter[int] = Counter()
    words_b: dict[int, list[str]] = {}
    max_b = 0
    for off, txt in messages:
        if off < 0 or not txt or noise_re.search(txt):
            continue
        b = int(off // window_s)
        if b > max_b:
            max_b = b
        raw_b[b] += 1
        wgt_b[b] += 2 if hype_re.search(txt) else 1
        words_b.setdefault(b, []).append(txt)

    if max_b < 3:
        return []
    vals = [float(wgt_b.get(i, 0)) for i in range(max_b + 1)]
    n = len(vals)
    sorted_v = sorted(vals)
    med = sorted_v[n // 2]
    std = math.sqrt(sum((v - med) ** 2 for v in vals) / n)
    if std < 1e-6:
        return []

    peaks: list[dict[str, Any]] = []
    for b in sorted(range(n), key=lambda i: vals[i], reverse=True):
        z = (vals[b] - med) / std
        if z < 2.0 or raw_b[b] < 6:
            break
        t_s = float(b * window_s)
        if any(abs(t_s - p["t_s"]) < min_gap_s for p in peaks):
            continue
        cnt: Counter[str] = Counter()
        for t in words_b.get(b, []):
            cnt.update(w.lower() for w in word_re.findall(t) if w.lower() not in stop)
        peaks.append({
            "t_s": round(t_s, 1),
            "msgs_10s": int(raw_b[b]),
            "z_score": round(z, 2),
            "top_words": [w for w, _ in cnt.most_common(4)],
        })
        if len(peaks) >= top_k:
            break
    return sorted(peaks, key=lambda x: x["t_s"])


def twitch_vod_chat_spikes(
    vod_id: str,
    *,
    duration_s: float = 0.0,
    max_seconds_budget: float = 35.0,
    top_k: int = 15,
) -> list[dict[str, Any]]:
    """Fetch Twitch VOD chat replay spikes via Twitch public GQL (fail-soft, time-bounded).

    For very long VODs, samples evenly across the VOD timeline if full pagination would exceed
    the time budget so ingest never stalls.
    """
    import requests

    vid = str(vod_id).strip()
    if not vid.isdigit():
        return []

    deadline = time.monotonic() + float(max_seconds_budget)
    messages: list[tuple[float, str]] = []
    sess = requests.Session()
    headers = {"Client-Id": TWITCH_WEB_CLIENT_ID}

    def _fetch_page(variables: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        payload = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _TWITCH_VOD_CHAT_HASH}},
        }]
        r = sess.post(TWITCH_GQL_URL, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        body = r.json()
        item = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else {})
        comments = ((item.get("data") or {}).get("video") or {}).get("comments") or {}
        edges = comments.get("edges") or []
        next_cursor = edges[-1].get("cursor") if edges and (comments.get("pageInfo") or {}).get("hasNextPage") else None
        return edges, next_cursor

    def _extract(edges: list[dict[str, Any]]) -> None:
        for e in edges:
            node = e.get("node") or {}
            off = node.get("contentOffsetSeconds")
            frags = ((node.get("message") or {}).get("fragments") or [])
            txt = "".join(str(f.get("text") or "") for f in frags).strip()
            if off is not None and txt:
                messages.append((float(off), txt))

    try:
        # Probe start of VOD first to verify persisted hash works
        edges, cursor = _fetch_page({"videoID": vid, "contentOffsetSeconds": 0})
        if not edges:
            return []
        _extract(edges)

        # For VODs > 2h, sample across evenly spaced offsets to cover the whole stream within budget
        if duration_s > 7200:
            step = max(300.0, duration_s / 60.0)
            off = step
            while off < duration_s and time.monotonic() < deadline:
                try:
                    e2, c2 = _fetch_page({"videoID": vid, "contentOffsetSeconds": int(off)})
                    _extract(e2)
                    if c2 and time.monotonic() < deadline:
                        e3, _ = _fetch_page({"videoID": vid, "cursor": c2})
                        _extract(e3)
                except Exception:
                    break
                off += step
        else:
            pages = 0
            while cursor and pages < 80 and time.monotonic() < deadline:
                edges, cursor = _fetch_page({"videoID": vid, "cursor": cursor})
                if not edges:
                    break
                _extract(edges)
                pages += 1
    except Exception:
        return []

    return _score_chat_messages(messages, top_k=top_k)
