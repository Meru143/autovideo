"""The learning loop's evidence (Plan 3b Task 2): which levers hold viewers, from each public video's retention at day 7
(stats.retention, averageViewPercentage). Medians and shrinkage keep one lucky video from steering the channel, and the
rollback rule judges the change in flight (FAILURE_MODES.md, loop)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from statistics import mean, median, quantiles

from .. import OUT, jobs
from ..stats import retention

MIN_VIDEOS = 10  # nothing is said before this many videos have day-7 retention
K = 3  # shrinkage: a lever's estimate is (n*mean + K*channel_median) / (n + K)
THIN = 3  # seen fewer times than this, a lever is never the top one: shrinkage alone lets one 95% video top it
UNDER, BEFORE = 5, 10  # the rollback rule: 5 videos made under a change against the 10 made before it


def _videos(conn, ws: str, root: Path) -> list[dict]:
    """The channel's published videos with day-7 retention, oldest made first."""
    with jobs._db(root) as c:
        made = dict(c.execute("select id, created_at from jobs where workspace = ?", (ws,)).fetchall())
    out = []
    for vid, job, lever, key, at in conn.execute("select video_id, job_id, lever, topic_key, at from published_videos "
                                                 "where workspace = ?", (ws,)).fetchall():
        r = retention(conn, ws, vid)
        if r is not None:
            out.append({"video_id": vid, "job": job, "lever": lever or "none", "key": key, "retention": r,
                        "made": made.get(job) or datetime.fromisoformat(at).timestamp()})
    return sorted(out, key=lambda v: (v["made"], v["video_id"]))


def _in_flight(vids: list[dict], record: list[dict]) -> dict | None:
    judged = {c.get("commit") for c in record if c.get("status") in ("kept", "reverted", "revert conflict")}
    applied = [c for c in record if c.get("status") == "applied" and c.get("commit") not in judged]
    if not applied:
        return None
    change = applied[-1]
    at = datetime.fromisoformat(change["at"]).timestamp()
    since, before = [v for v in vids if v["made"] > at], [v for v in vids if v["made"] <= at]
    out = {"change": change.get("summary"), "commit": change.get("commit"), "videos_since": len(since),
           "verdict": "waiting"}
    if len(since) >= UNDER and len(before) >= BEFORE:
        under = median(v["retention"] for v in since[:UNDER])
        floor = quantiles([v["retention"] for v in before[-BEFORE:]], n=4, method="inclusive")[0]
        out.update(median_since=under, floor=round(floor, 2), verdict="revert" if under < floor else "keep")
    return out


def evidence(conn, ws: str, now: datetime, *, root: Path = OUT, record: list[dict] | None = None) -> dict | None:
    """None until MIN_VIDEOS videos have day-7 retention; then the channel's median and spread (MAD), each lever's n,
    raw median and shrunk estimate, the top lever (never a thin one), the best and worst three, and the change in
    flight with its verdict: keep, revert or waiting."""
    vids = _videos(conn, ws, root)
    if len(vids) < MIN_VIDEOS:
        return None
    rets = [v["retention"] for v in vids]
    med = median(rets)
    levers = {}
    for lever in sorted({v["lever"] for v in vids}):
        xs = [v["retention"] for v in vids if v["lever"] == lever]
        levers[lever] = {"n": len(xs), "raw_median": median(xs), "thin": len(xs) < THIN,
                         "shrunk": round((len(xs) * mean(xs) + K * med) / (len(xs) + K), 2)}
    solid = [k for k, v in levers.items() if not v["thin"]]
    ranked = sorted(vids, key=lambda v: (-v["retention"], v["video_id"]))
    return {"videos": vids, "median": med, "mad": median(abs(x - med) for x in rets), "levers": levers,
            "top": max(solid, key=lambda k: (levers[k]["shrunk"], k)) if solid else None,
            "best": ranked[:3], "worst": sorted(vids, key=lambda v: (v["retention"], v["video_id"]))[:3],
            "in_flight": _in_flight(vids, record or [])}
