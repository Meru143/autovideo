"""The console's reads (console Task 2): factory state in the API contract's shapes (the plan's "API contract"). They
hold no rules: which column a video sits in, whether the tick will retry it, when it posts and whether its channel is
capped all come from the scheduler's own functions. Jobs and the topic store are read through the factory's own
accessors, each with its busy timeout. A job folder that can't be read is skipped and logged, never the whole board
(Review Focus 5); a half-written tick.log, tick_state.json or quota.json is no data for that part, never an error."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import claude_call, jobs, scheduler, settings as factory_settings
from ..lessons import AUTHOR as LESSONS
from ..loop.apply import AUTHOR as LOOP
from ..pipeline import PIPELINES
from ..topics import store
from ..workspace import channels_root, format_of, instagram_of

RUNNING = timedelta(minutes=20)  # the tick runs every 15 minutes: an older last report means it stopped
ON_AIR = timedelta(days=7)  # the board's live column
AHEAD = timedelta(days=14)  # how far ahead a ready video's slot is looked for
TAIL = 1 << 16  # tick.log's last report sits well inside its last 64 KB
RECORDS = ("topic", "metadata", "review", "approval", "upload", "instagram")  # <job>/<name>.json, as a card reads them
TICK = ("channel", "skipped_slot", "late_slot", "refused", "broken")  # what the health bar shows of the last report
AUTHORS = {LESSONS.split(" <")[0]: "lessons", LOOP.split(" <")[0]: "loop"}
# One thumbnail decoder at a time (final review, 10 Nov): a first board asks for 10-20 at once, each decoding a
# 1080x1920 video, maybe beside two renders near Windows' commit limit; opening the console must never hurt the factory.
# ponytail: one lock in this process; a second console open at once would decode beside it.
_DECODER = threading.Lock()


def _json(path: Path):
    """A file the factory rewrites in place: None while it's missing or half-written."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lines(path: Path) -> list[dict]:
    """An append-only log's records; a line still being written is passed over."""
    out = []
    for x in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        try:
            out.append(json.loads(x))
        except ValueError:
            continue
    return out


def _iso(t) -> str | None:
    """A time as ISO UTC. The factory writes epoch seconds (the jobs table), aware ISO strings and, from jobs.run and
    jobs.reset, naive local ones."""
    if t is None or t == "":
        return None
    t = datetime.fromtimestamp(t, timezone.utc) if isinstance(t, (int, float)) else datetime.fromisoformat(t)
    return t.astimezone(timezone.utc).isoformat()


def _slot(t: datetime | None, tz: str) -> dict | None:
    """A slot as the contract shows it: ISO UTC, the channel's own clock, and the city its timezone names."""
    return t and {"at": t.astimezone(timezone.utc).isoformat(), "local": t.astimezone(ZoneInfo(tz)).strftime("%H:%M"),
                  "city": tz.rsplit("/", 1)[-1].replace("_", " ")}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def _rel(repo: Path, path: Path) -> str:
    return Path(os.path.relpath(path, repo)).as_posix()


def _by(author: str, me: str) -> str:
    """Who changed a file: the lessons loop, the learning loop, the repo's own identity (the owner), or someone else."""
    return AUTHORS.get(author) or ("you" if author == me else "other")


def _channels() -> dict[str, dict]:
    """The channels the tick serves (those with a schedule), read as the tick reads them."""
    return dict(scheduler._scheduled())


def _summary(name: str, ws: dict) -> dict:
    sc = ws["schedule"]
    return {"name": name, "display_name": ws.get("display_name", name), "color": (ws.get("console") or {}).get("color"),
            "format": format_of(ws)["pipeline"], "paused": bool(sc.get("paused")), "slots": sc["slots"],
            "timezone": sc["timezone"], "max_per_day": sc.get("max_per_day"), "instagram": instagram_of(ws)["enabled"]}


def _state(root: Path) -> dict:
    state = _json(root / "tick_state.json")
    return state if isinstance(state, dict) else {}


def _facts(root: Path, name: str, ws: dict, state: dict, now: datetime) -> dict:
    """What every card of a channel shares, from the scheduler's own functions: its stages, the stopped video the tick
    would retry, and each ready video's slot. Ready videos take the channel's next slots in the order the tick posts
    them (best topic first, as _eligible picks), passing over a slot where the channel is capped (_capped: it has had
    its post that day)."""
    sc, tz = ws["schedule"], ws["schedule"]["timezone"]
    refused = {j: why for j in scheduler._finished(root, name) if (why := scheduler._refused(root, j))}
    ready = [j for j in scheduler._finished(root, name) if j not in refused]
    ready.sort(key=lambda j: -float(scheduler._read(root / "jobs" / j / "topic.json").get("score") or 0))
    st = (state.get("channels") or {}).get(name) or {}
    free = iter(() if sc.get("paused") else scheduler.slots_between(  # the tick serves no paused channel
        sc["slots"], tz, datetime.fromisoformat(st["slot_done"]) if st.get("slot_done") else now, now + AHEAD))
    # ponytail: one ready video per free slot; on a 2-slot day the second video, capped by the first, isn't simulated
    slots = {j: next((t for t in free if not scheduler._capped(root, {"job": j, "ws": name, "tz": tz}, "youtube", t)),
                     None) for j in ready}
    return {"stages": [n for n, *_ in PIPELINES[format_of(ws)["pipeline"]]], "tz": tz, "ready": slots,
            "refused": refused, "retry": scheduler._retryable(root, name)}


def _records(d: Path) -> dict:
    """The job's JSON records, None when absent. One that won't parse (half-written, cut off) raises: the folder can't
    be read."""
    return {k: json.loads(p.read_text(encoding="utf-8")) if (p := d / f"{k}.json").exists() else None
            for k in RECORDS}


def _place(state: str, posted: bool, ready: bool, retrying: bool, refused: bool) -> tuple[str, str, str | None] | None:
    """The board's column, lamp and note, by the contract's table; None for a job the board leaves out
    (dropped). A finished video the upload guard refuses can't post until someone acts: it needs the owner."""
    if posted:  # a dropped video that has upload.json or instagram.json is on air all the same
        return "live", "live", None
    if state in ("failed", "quarantined"):
        return "making", "attention", "Stopped · retrying" if retrying else "Stopped · needs you"
    if state.split(":")[0] in scheduler.IN_PROGRESS:
        return "making", "making", None
    if refused:
        return "making", "attention", "Stopped · needs you"
    return ("ready", "ready", None) if ready else None


def _card(root: Path, jid: str, row: tuple, rec: dict, facts: dict, views: dict, now: datetime,
          board: bool = True) -> dict | None:
    name, state, updated = row
    d, up, approval = root / "jobs" / jid, rec["upload"], rec["approval"] or {}
    # posted on YouTube, or a recorded Reel alone (final review, 10 Nov): the Reel is public even when YouTube failed
    posted = up if up is not None else rec["instagram"]
    place = _place(state, posted is not None, jid in facts["ready"], jid == facts["retry"],
                  jid in facts["refused"])
    if place is None and board:
        return None
    column, lamp, note = place or ("making", "attention", "Dropped" if state == "dropped" else "Stopped · needs you")
    cur = next((n for n in facts["stages"] if not (d / f"{n}.done.json").exists()), None)
    making = column == "making" and cur is not None
    slot = facts["ready"].get(jid) if column == "ready" else None
    if column == "live" and approval.get("slot"):  # the slot the tick posted it for
        slot = datetime.fromisoformat(approval["slot"])
    return {"id": jid, "channel": name,
            "title": (rec["metadata"] or {}).get("title") or (rec["topic"] or {}).get("key") or jid,
            "column": column, "lamp": lamp, "stage": cur if making else None,
            "step": [facts["stages"].index(cur) + 1, len(facts["stages"])] if making else None, "note": note,
            "slot": _slot(slot, facts["tz"]),
            "posted": None if posted is None else {"at": _iso(posted.get("at")), "youtube": (up or {}).get("video_id"),
                                                   "instagram": rec["instagram"] is not None},
            "views": None if up is None else views.get(up.get("video_id")),
            "thumb": f"/api/videos/{jid}/thumb" if (d / "final.mp4").exists() or (d / "sheet.png").exists() else None,
            "updated": _iso(updated),
            "capped": column == "ready" and scheduler._capped(root, {"job": jid, "ws": name, "tz": facts["tz"]},
                                                              "youtube", now)}


def _views(root: Path) -> dict:
    """Each video's latest live view count (stats.pull: what Studio shows)."""
    conn = store.connect(root)
    try:
        return {r["video_id"]: r["live_views"] for r in conn.execute(
            "select video_id, live_views from video_stats where live_views is not null order by as_of")}
    finally:
        conn.close()


def _cards(root: Path, now: datetime, only: str | None = None) -> list[dict]:
    """The board's cards, the tick's jobs only (topic.json), oldest made first."""
    tick = set(scheduler._tick_jobs(root, only))
    with jobs._db(root) as c:
        rows = [r for r in c.execute("select id, workspace, state, updated_at from jobs order by created_at")
                if r[0] in tick]
    state, chans, facts, views, out = _state(root), _channels(), {}, _views(root), []
    for jid, *row in rows:
        try:
            if row[0] not in facts:
                facts[row[0]] = _facts(root, row[0], chans[row[0]], state, now)
            card = _card(root, jid, tuple(row), _records(root / "jobs" / jid), facts[row[0]], views, now)
        except Exception as e:  # one job's folder never takes the board down (Review Focus 5)
            print(f"console: skipped job {jid}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if card:
            out.append(card)
    return out


def videos(root: Path, now: datetime | None = None) -> dict:
    """The board: being made oldest first, ready by slot, on air (the last 7 days) newest first."""
    now = now or datetime.now(timezone.utc)
    cards, since = _cards(root, now), (now - ON_AIR).astimezone(timezone.utc).isoformat()  # ISO UTC sorts as time
    col = {k: [c for c in cards if c["column"] == k] for k in ("making", "ready", "live")}
    col["ready"].sort(key=lambda c: (c["slot"] is None, c["slot"] and c["slot"]["at"]))
    col["live"] = sorted((c for c in col["live"] if (c["posted"]["at"] or "") >= since),
                         key=lambda c: c["posted"]["at"], reverse=True)
    return {"videos": col["making"] + col["ready"] + col["live"]}


def _events(d: Path, rec: dict, refused: list[str]) -> list[dict]:
    """What happened to the video, oldest first: its error, the tick's and the owner's actions, lessons, the review's
    verdicts and its posts. A verdict carries no time of its own: it takes its file's."""
    out = []

    def add(at, kind: str, text: str) -> None:
        out.append({"at": _iso(at), "kind": kind, "text": text})
    for r in _lines(d / "operator.jsonl"):
        if r.get("action") == "reset":
            add(r.get("at"), "retry", f"Retried by {r.get('by')}, from {r.get('from_state')}")
        elif r.get("action") == "drop":
            add(r.get("at"), "drop", f"Dropped by {r.get('by')}")
        elif r.get("action") == "last_resort":
            add(r.get("at"), "review", "Taken as the last resort; blocked for: "
                + "; ".join(r.get("blocked_for") or []))
    for r in _lines(d / "lessons.jsonl"):
        add(r.get("at"), "lesson", r.get("error") or (r.get("summary") or "No change") + (
            f" ({', '.join(r['files'])})" if r.get("files") else ""))
    hist = d / "review_history.json"
    verdicts, src = (_json(hist), hist) if hist.exists() else ([rec["review"]], d / "review.json")
    for v in verdicts or []:
        if v and v.get("block"):
            add(src.stat().st_mtime, "review", f"Blocked ({v.get('severity') or 'major'}): "
                + "; ".join(v.get("reasons") or []))
        elif v:
            add(src.stat().st_mtime, "review", "Passed" + (": " + "; ".join(v["defects"]) if v.get("defects") else ""))
    if (err := d / "error.txt").exists():
        lines = err.read_text(encoding="utf-8", errors="replace").strip().splitlines() or [""]
        add(err.stat().st_mtime, "error", lines[0] if len(lines) == 1 else f"{lines[0]} {lines[-1]}")
    for why in refused:  # a finished video the upload guard won't let post
        add(d.stat().st_mtime, "error", why)
    if rec["upload"] is not None:
        add(rec["upload"].get("at"), "post",
            f"Posted on YouTube ({rec['upload'].get('privacy') or 'privacy unchecked'})")
    if rec["instagram"] is not None:
        add(rec["instagram"].get("at"), "post", "Posted on Instagram")
    return sorted(out, key=lambda e: e["at"] or "")


def video(root: Path, jid: str, now: datetime | None = None) -> dict | None:
    """One video: its card, its stages with their done times, what happened, its script and what can be done to it."""
    now = now or datetime.now(timezone.utc)
    with jobs._db(root) as c:
        row = c.execute("select workspace, state, updated_at from jobs where id = ?", (jid,)).fetchone()
    if row is None:
        return None
    d, rec = root / "jobs" / jid, _records(root / "jobs" / jid)
    facts = _facts(root, row[0], _channels()[row[0]], _state(root), now)
    card = _card(root, jid, row, rec, facts, _views(root), now, board=False)
    first = next((n for n in facts["stages"] if not (d / f"{n}.done.json").exists()), None)
    now_on = {"attention": "stopped", "making": "current"}.get(card["lamp"], "todo")
    stages = [{"name": n, "done_at": _iso((_json(d / f"{n}.done.json") or {}).get("at")), "state": "done"}
              if (d / f"{n}.done.json").exists() else {"name": n, "done_at": None,
                                                       "state": now_on if n == first else "todo"}
              for n in facts["stages"]]
    ready = card["column"] == "ready"
    return {**card, "stages": stages, "events": _events(d, rec, facts["refused"].get(jid, [])),
            "script": (d / "narration.txt").read_text(encoding="utf-8") if (d / "narration.txt").exists() else None,
            "media": f"/api/videos/{jid}/media" if (d / "final.mp4").exists() else None,
            "can": {"post": ready and not card["capped"], "anyway": ready and card["capped"],
                    "retry": row[1] in ("failed", "quarantined"),
                    "drop": row[1] not in ("dropped", jobs.POSTING) and card["column"] != "live"}}  # jobs.drop's rule


def thumb(root: Path, jid: str) -> Path | None:
    """A frame at 0.1 s of final.mp4, 360 px wide, cached beside it as console-thumb.jpg (made again when the video is
    newer), by one ffmpeg at a time (_DECODER); with no video, or one ffmpeg can't read yet, the contact sheet; else
    None."""
    d = root / "jobs" / jid
    final, jpg, sheet = d / "final.mp4", d / "console-thumb.jpg", d / "sheet.png"
    if final.is_file() and (not jpg.exists() or jpg.stat().st_mtime < final.stat().st_mtime):
        part = d / f"console-thumb.{threading.get_ident()}.jpg"  # two requests at once each write their own
        with _DECODER:  # a cached thumbnail never waits: only a request that decodes takes it
            r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.1", "-i", str(final), "-frames:v", "1",
                                "-vf", "scale=360:-2", str(part)], capture_output=True, timeout=60)
        try:
            if r.returncode == 0:
                os.replace(part, jpg)
        except OSError:  # Windows: the old one is being sent; it serves this time
            pass
        part.unlink(missing_ok=True)
    return jpg if final.is_file() and jpg.exists() else sheet if sheet.is_file() else None


def channels() -> dict:
    return {"channels": [_summary(n, ws) for n, ws in scheduler._scheduled()]}


def channel(root: Path, repo: Path, name: str, now: datetime | None = None) -> dict | None:
    """A channel: its settings, each playbook with who changed it last (git), its topic settings, and its videos on
    air, newest first."""
    ws = _channels().get(name)
    if ws is None:
        return None
    me, books = _git(repo, "config", "user.name").stdout.strip(), []
    for key, rel in ws.get("playbooks", {}).items():
        path = Path(ws["dir"]) / rel
        last = _git(repo, "log", "-1", "--format=%H|%aI|%an", "--", str(path)).stdout.strip()
        sha, at, author = last.split("|", 2) if last else ("", "", "")
        books.append({"key": key, "path": _rel(repo, path),
                      "changed": {"by": _by(author, me), "at": _iso(at), "sha": sha} if last else None})
    tp = ws.get("topics", {})
    on_air = sorted((c for c in _cards(root, now or datetime.now(timezone.utc), name) if c["column"] == "live"),
                    key=lambda c: c["posted"]["at"] or "", reverse=True)
    return {**_summary(name, ws), "playbooks": books,
            "topics": {"levers": tp.get("levers", []), "min_score": scheduler._rank(ws)["min_score"],
                       "ideas_per_pass": tp.get("ideas_per_pass", 6)},  # briefs.py's default
            "recent": on_air[:10]}


def playbook(repo: Path, name: str, key: str) -> dict | None:
    """A playbook's text, its last commit (the base a save must still match) and its history."""
    ws = _channels().get(name)
    rel = (ws or {}).get("playbooks", {}).get(key)
    if rel is None:
        return None
    path, me = Path(ws["dir"]) / rel, _git(repo, "config", "user.name").stdout.strip()
    log = [x.split("|", 3) for x in _git(repo, "log", "-n", "50", "--format=%H|%aI|%an|%s", "--",
                                         str(path)).stdout.splitlines() if x]
    return {"key": key, "text": path.read_text(encoding="utf-8"), "base": log[0][0] if log else "",
            "history": [{"sha": h, "at": _iso(at), "by": _by(an, me), "subject": s} for h, at, an, s in log]}


def topics(root: Path, name: str) -> dict | None:
    """The channel's briefs: queued first, best score first, then the rest newest first."""
    if name not in _channels():
        return None
    conn = store.connect(root)
    try:
        rows = conn.execute("select id, key, brief, score, status, created_at from briefs where workspace = ? "
                            "order by id desc limit 200", (name,)).fetchall()
    finally:
        conn.close()
    out = [{"id": r["id"], "key": r["key"], "lever": json.loads(r["brief"]).get("lever"), "score": r["score"],
            "status": r["status"], "created": _iso(r["created_at"])} for r in rows]
    return {"topics": sorted(out, key=lambda t: (t["status"] != "queued",
                                                 -t["score"] if t["status"] == "queued" else 0))}


def changes(repo: Path) -> dict:
    """The last 100 commits under the channels folder by the lessons loop or the learning loop, or with a console(
    subject. One is reverted when a later commit says it reverts it (git revert's own line), unless that revert was
    itself reverted (an undo taken back)."""
    top = _rel(repo, channels_root())
    out = _git(repo, "log", "-n", "1000", "--format=%x1e%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1f", "--name-only", "--",
               top).stdout
    commits = [c.split("\x1f") for c in out.split("\x1e")[1:]]
    undone: set[str] = set()
    for c in commits:  # newest first: a revert reverted later is already known when it is reached, and undoes nothing
        if c[0] not in undone:
            undone.update(re.findall(r"This reverts commit ([0-9a-f]{40})", c[4]))
    found = []
    for sha, at, author, subject, _, names in commits:
        if author not in AUTHORS and not subject.startswith("console("):
            continue
        files = names.strip().splitlines()
        chans = {f[len(top) + 1:].split("/")[0] for f in files if f.startswith(top + "/")}
        found.append({"sha": sha, "at": _iso(at), "by": AUTHORS.get(author, "you"),
                      "channel": chans.pop() if len(chans) == 1 else None, "subject": subject, "files": files,
                      "reverted": sha in undone})
    return {"changes": found[:100]}


def diff(repo: Path, sha: str) -> dict | None:
    r = _git(repo, "show", "--format=", "--no-color", "--no-ext-diff", f"{sha}^{{commit}}", "--",
             _rel(repo, channels_root()))
    return {"diff": r.stdout} if r.returncode == 0 else None


def _title(root: Path, jid: str | None) -> str | None:
    if jid is None:  # a stats row with no published_videos row
        return None
    d = root / "jobs" / jid
    return (_json(d / "metadata.json") or {}).get("title") or (_json(d / "topic.json") or {}).get("key")


def stats(root: Path) -> dict:
    """Each published video's latest numbers (live views and likes; the analytics once they arrive) and each channel's
    views per day, summed over its videos (each carried forward past a day it has no row)."""
    conn = store.connect(root)
    try:
        rows = conn.execute("select s.*, p.job_id from video_stats s left join published_videos p "
                            "using (workspace, video_id) order by s.as_of").fetchall()
    finally:
        conn.close()
    latest, seen, days = {}, {}, {}
    for r in rows:
        latest[r["workspace"], r["video_id"]] = r
        seen.setdefault(r["workspace"], {}).setdefault(r["as_of"], {})[r["video_id"]] = r["live_views"] or 0
    for ws, per in seen.items():  # cumulative totals: a video with no row on a date keeps its last known count
        known = {}
        days[ws] = {}
        for date in sorted(per):
            known.update(per[date])
            days[ws][date] = sum(known.values())
    return {"videos": [{"channel": r["workspace"], "video_id": r["video_id"],
                        "title": _title(root, r["job_id"]) or r["video_id"], "views": r["live_views"],
                        "likes": r["live_likes"], "avg_view_pct": r["avg_view_pct"],
                        "engaged_views": r["engaged_views"], "age_days": r["age_days"], "as_of": r["as_of"]}
                       for r in latest.values()],
            "channels": {ws: [{"date": k, "views": v} for k, v in sorted(per.items())] for ws, per in days.items()}}


def _last_report(log: Path) -> dict | None:
    """The tick's last report in tick.log: `python -m factory tick` prints one per tick, indented, so each opens with a
    line holding only "{" (its stderr lands there too). A busy tick's report has no time and is passed over; one still
    being written is no data."""
    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - TAIL))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for m in reversed(list(re.finditer(r"^\{\s*$", text, re.M))):
        try:
            report = json.JSONDecoder().raw_decode(text, m.start())[0]
        except ValueError:
            return None
        if isinstance(report, dict) and report.get("at"):
            return report
    return None


def health(root: Path, now: datetime | None = None) -> dict:
    """The health bar: the tick running (its last report under 20 minutes old), the videos being made (the board's
    Being made column, by the same placement, stopped ones included: ruling T6-a), the last report's slots, refusals
    and broken channels, the quota reading, and the account the factory signs in as."""
    now = now or datetime.now(timezone.utc)
    rep = _last_report(root / "tick.log")
    rl = (_json(root / "quota.json") or {}).get("rate_limits") or {}
    week, hour = rl.get("seven_day") or {}, rl.get("five_hour") or {}
    who = (_json(claude_call._factory_config() / ".claude.json") or {}).get("oauthAccount") or {}
    return {"running": rep is not None and now - datetime.fromisoformat(rep["at"]) < RUNNING,
            "last_tick": rep and _iso(rep["at"]),
            "being_made": sum(c["column"] == "making" for c in _cards(root, now)),
            "tick": rep and {k: rep[k] for k in TICK if k in rep},
            "quota": {"week": week.get("used_percentage"), "five_hour": hour.get("used_percentage"),
                      "week_resets": week.get("resets_at")},
            "account": who.get("emailAddress")}


def settings(root: Path) -> dict:
    """The Settings page: the factory's settings (or, when the file won't load, the reason, beside the read-only parts),
    the choices the editor offers, the sign-in's email and plan, the tick's billing and the latest quota reading. No
    token field is ever read out of the sign-in files."""
    s = factory_settings
    config = claude_call._factory_config()
    who = (_json(config / ".claude.json") or {}).get("oauthAccount") or {}
    plan = (_json(config / ".credentials.json") or {}).get("claudeAiOauth") or {}
    rep = _last_report(root / "tick.log") or {}
    out = {"steps": s.STEPS, "models": s.MODELS, "efforts": s.EFFORTS, "counts": s.COUNTS,
           "account": {"email": who.get("emailAddress"), "plan": plan.get("subscriptionType")},
           "billing": rep.get("billing") or "subscription",
           "quota": (_json(root / "quota.json") or {}).get("rate_limits")}
    try:
        out["settings"] = s.load()
    except ValueError as e:
        out["error"] = str(e)
    return out
