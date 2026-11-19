"""Each public video's numbers, pulled once a day (learning loop Task 1; operator, 9 Nov: collect now, so the loop has
its 10 videos at day 7 when it's built), through each channel's own token, into factory.db: one row per video per day
for its first 28 days. Each row holds the live views, likes and comments (the Data API's counters, what Studio shows)
and, once they arrive 1-3 days later, the Analytics API's numbers (9 Nov: the analytics alone read 10 views where
Studio showed 1,002). The loop's score, later, is averageViewPercentage at day 7 or later; nothing is ever imputed
(FAILURE_MODES.md, stats)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import OUT
from .publish import _rows, _session
from .topics import store
from .workspace import channels, load

# The basic-stats report allows these together with no dimension and one video filter (channel_reports, 9 Nov).
# engagedViews keeps YouTube's old Shorts count; views counts every play and replay since 2025.
METRICS = ("views", "engagedViews", "averageViewPercentage", "averageViewDuration", "likes", "shares", "subscribersGained")
COLUMNS = ("views", "engaged_views", "avg_view_pct", "avg_view_s", "likes", "shares", "subs_gained")
LIVE = {"viewCount": "live_views", "likeCount": "live_likes", "commentCount": "live_comments"}
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
MAX_AGE_DAYS = 28


class MissingScope(RuntimeError):
    """The channel's token can't read analytics: the operator signs in again on purpose."""


def _ok(r) -> None:
    if r.status_code == 403 and "insufficient" in r.text.lower():
        raise MissingScope("the YouTube token lacks yt-analytics.readonly: re-run tools/youtube_auth.py for this channel")
    if r.status_code >= 400:  # the API's quota or a 5xx: stop here, keep what's saved, try again next tick
        raise RuntimeError(f"YouTube answered {r.status_code}: {r.text[:200]}")


def sync(conn, root: Path = OUT) -> None:
    """Every YouTube upload in the ledger is a published video, with its job's brief and lever when it has them."""
    with conn:
        for r in _rows(root / "uploads.jsonl"):
            if not (r.get("video_id") and r.get("job") and r.get("at")):
                continue
            ws = _workspace(root, r["job"])
            if ws is None:
                continue
            topic = root / "jobs" / r["job"] / "topic.json"
            t = json.loads(topic.read_text(encoding="utf-8")) if topic.exists() else {}
            conn.execute("insert or ignore into published_videos values (?, ?, ?, ?, ?, ?, ?)",
                         (ws, r["video_id"], r["job"], t.get("brief_id"), t.get("lever"), t.get("key"), r["at"]))


def _workspace(root: Path, job_id: str) -> str | None:
    from . import jobs
    try:
        return jobs.load(job_id, root=root).workspace
    except KeyError:
        return None


def pull(conn, ws: dict, now: datetime, *, root: Path = OUT, session=None) -> dict:
    """The day's numbers for the channel's public videos aged 1 to 28 days, oldest first: the live counters for all of
    them in one call, then one analytics query each (lifetime to date). Returns {videos, rows, missing, private, views}:
    rows counts the videos whose analytics have arrived. A refused token or an API error stops it, keeping saved rows."""
    sync(conn, root)
    due = {v: datetime.fromisoformat(at) for v, at in conn.execute(
        "select video_id, at from published_videos where workspace = ?", (ws["name"],))
        if 1 <= (now - datetime.fromisoformat(at)).days <= MAX_AGE_DAYS}
    if not due:
        return {"videos": 0, "rows": 0, "missing": [], "private": []}
    sess = session or _session(ws)
    ids, live = sorted(due), {}
    for off in range(0, len(ids), 50):  # YouTube Data API videos.list accepts at most 50 IDs per request
        r = sess.get(VIDEOS, params={"part": "status,statistics", "id": ",".join(ids[off:off + 50])}, timeout=60)
        _ok(r)
        for i in r.json().get("items", []):
            if i.get("status", {}).get("privacyStatus") == "public":
                live[i["id"]] = {col: int(i.get("statistics", {}).get(k) or 0) for k, col in LIVE.items()}
    public = set(live)
    got = []
    for vid in sorted(public, key=lambda v: due[v]):
        r = sess.get(ANALYTICS, params={"ids": "channel==MINE", "startDate": due[vid].date().isoformat(),
                                        "endDate": now.date().isoformat(), "metrics": ",".join(METRICS),
                                        "filters": f"video=={vid}"}, timeout=60)
        _ok(r)
        body = r.json()
        rows = body.get("rows") or []  # none yet: the analytics run 1-3 days behind, so tomorrow's pull asks again
        values = dict(zip([h["name"] for h in body.get("columnHeaders", [])], rows[0])) if rows else {}
        cols = (*LIVE.values(), *COLUMNS)
        with conn:
            conn.execute(f"insert or replace into video_stats (workspace, video_id, as_of, age_days, {', '.join(cols)}) "
                         f"values (?, ?, ?, ?, {', '.join('?' * len(cols))})",
                         (ws["name"], vid, now.date().isoformat(), (now.date() - due[vid].date()).days,
                          *live[vid].values(), *[values.get(m) for m in METRICS]))
        if values:
            got.append(vid)
    return {"videos": len(public), "rows": len(got), "missing": sorted(public - set(got)),
            "private": sorted(set(due) - public), "views": {v: live[v]["live_views"] for v in sorted(public)}}


def retention(conn, ws: str, video_id: str) -> float | None:
    """The loop's score: averageViewPercentage in the first analytics row at day 7 or later; None until there is one."""
    row = conn.execute("select avg_view_pct from video_stats where workspace = ? and video_id = ? and age_days >= 7 "
                       "and avg_view_pct is not null order by age_days limit 1", (ws, video_id)).fetchone()
    return row[0] if row else None


def daily(now: datetime | None = None, *, root: Path = OUT, session_for=None) -> dict:
    """After the tick: each channel with a YouTube token, once a day. {channel: report or error}; never raises."""
    now = now or datetime.now(timezone.utc)
    mark = root / "stats_state.json"
    done = json.loads(mark.read_text(encoding="utf-8")) if mark.exists() else {}
    day, out = now.strftime("%Y%m%d"), {}
    conn = store.connect(root)
    try:
        for name in channels():
            if done.get(name) == day:
                continue
            try:
                ws = load(name, need=())
            except ValueError:
                continue  # an unreadable channel is the tick's to report
            if not ws.get("youtube_token"):
                continue
            try:
                out[name] = pull(conn, ws, now, root=root, session=session_for(ws) if session_for else None)
                done[name] = day
            except Exception as e:  # reported in the tick log; the next tick tries this channel again
                out[name] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        conn.close()
    tmp = mark.with_suffix(mark.suffix + ".tmp")
    tmp.write_text(json.dumps(done), encoding="utf-8")
    os.replace(tmp, mark)
    return out
