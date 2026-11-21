"""The tick (Plan 3a Task 6; design 5.6, 5.4.8; FAILURE_MODES.md, topics 17-42). Task Scheduler runs `python -m factory
tick` every 15 minutes. Each tick resumes a crashed upload, then serves one channel: one with a due slot and something
to post first, else the one whose next uncovered slot comes soonest, else the next in turn (operator, 9 Nov: "one video
posted daily to each channel no matter what"). At a due slot it posts the best finished video the upload guard clears,
with nobody approving it (operator, 7 Nov: "remove telegram and my approval, upload directly"); with none, the slot
stays open 12 h for a video that lands late, and a video blocked for minor reasons only posts as the last resort. It
retries a failed or blocked job once by itself, starts one job when slots are uncovered, runs a topic pass when the
backlog is short, and expires stale briefs. Jobs run as their own processes; the tick returns at once. It only ever handles jobs it made
(each has topic.json), never a hand-run test job."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil

from . import OUT, ROOT, instagram, jobs, procs, settings
from .publish import _rows, guard
from .topics import briefs, store
from .workspace import channels, load

HORIZON, PUBLISH_TRIES = timedelta(hours=48), 3
# Uploads per channel and platform (5.4.8, the first 30 days): one a day in its timezone, never 2 in 24 h.
# ponytail: fixed caps; the ramp rule raises them per channel, a workspace setting once it's built.
CAP_DAY, CAP_24H = 1, 2
FINAL = {"done", "quarantined", "failed", "waiting_quota", "uploaded_private", "dropped"}
IN_PROGRESS = {"waiting_quota", "queued", "running"}
# Daily, no matter what (operator, 9 Nov): a slot with nothing to post stays open this long for a video that lands late.
# A failed or blocked video's retries by the tick itself are factory/settings.json's auto_retries.
LATE = timedelta(hours=12)
# Videos at once, one per channel (operator, 9 Nov): their Claude steps run remotely, and factory/gpu.py keeps the heavy
# GPU work to one at a time. Up to settings' videos_at_once: two always; a third only with ROOM_GB of memory headroom
# (9 Nov: with two running, 2.7 GB of Windows' commit limit was left on the 13.9 GB laptop, and past the limit jobs
# crash mid-way).
PARALLEL, ROOM_GB = 2, 4.0
# The console's actions (9 Nov) wait this long for a tick to let go of its lock, then answer busy.
LOCK_WAIT_S = 30


class Busy(RuntimeError):
    """The tick lock stayed held past LOCK_WAIT_S: a tick, or another console action, is running."""


class NotReady(RuntimeError):
    """Post now refused: the upload guard's reasons, or the video isn't finished."""


class AlreadyPosted(RuntimeError):
    """Post now found the video's upload.json."""


def slots_between(slots: list[str], tz: str, start: datetime, end: datetime) -> list[datetime]:
    """The channel's slot times in (start, end], in UTC (zoneinfo handles the clock changes)."""
    zone, out, day = ZoneInfo(tz), [], start.astimezone(ZoneInfo(tz)).date() - timedelta(days=1)
    while day <= end.astimezone(zone).date():
        for hm in slots:
            h, m = (int(x) for x in hm.split(":"))
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=zone).astimezone(timezone.utc)
            if start < t <= end:
                out.append(t)
        day += timedelta(days=1)
    return sorted(out)


def spawn(job_id: str, *, cmd: list[str] | None = None, root: Path = OUT) -> int:
    """Start the job to its review as its own process with this process's env (FACTORY_BILLING included, operator
    5 Nov); returns its PID at once. Detached, so the tick (and Task Scheduler's instance) can end while it runs."""
    log = root / "jobs" / job_id / "tick_run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = cmd or [sys.executable, "-m", "factory", "job", "run", job_id, "--until", "prepublic"]
    base = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with log.open("ab") as out:
        for flags in ((base | 0x01000000, base) if os.name == "nt" else (base,)):  # 0x01000000: leave Task Scheduler's job
            try:
                return subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        env={**os.environ, "FACTORY_OUT": str(root)}, creationflags=flags).pid
            except OSError:
                continue
    raise RuntimeError(f"could not start job {job_id}")


def _upload(job_id: str) -> str:
    from .pipeline import run_job
    state = run_job(job_id)  # the remaining stage: publish (per the channel's mode, idempotent per job, resumable)
    if state != "uploaded_private":
        raise RuntimeError(f"publish ended {state}")
    return state


def _reel(job_id: str, root: Path = OUT) -> dict:
    """The Instagram leg (Plan 3c): the finished video as a Reel, with the caption the metadata stage wrote."""
    job = jobs.load(job_id, root=root)
    return instagram.publish_reel(job, job.dir / "final.mp4", (job.dir / "instagram_caption.txt").read_text(
        encoding="utf-8"), graph=instagram.Graph(job.workspace))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _tick_jobs(root: Path, ws: str | None = None, states: set[str] | None = None) -> list[str]:
    """Jobs the tick made (topic.json), optionally for one channel and in some states, oldest first."""
    with jobs._db(root) as c:
        rows = c.execute("select id, workspace, state from jobs order by id").fetchall()
    return [j for j, w, s in rows if (ws is None or w == ws) and (states is None or s in states or
            any(s.startswith(x + ":") for x in states)) and (root / "jobs" / j / "topic.json").exists()]


def _finished(root: Path, ws: str | None = None) -> list[str]:
    """Made, reviewed and not yet uploaded. Ordered newest-first when the channel sets schedule.publish_order='newest'."""
    out = [j for j in _tick_jobs(root, ws, {"done"}) if (root / "jobs" / j / "prepublic.done.json").exists()
           and not (root / "jobs" / j / "upload.json").exists()]
    if ws and len(out) > 1:
        try:
            if load(ws).get("schedule", {}).get("publish_order") == "newest":
                out.reverse()
        except Exception:
            pass
    return out


def _alive(running: dict | None, root: Path) -> bool:
    if not running:
        return False
    try:
        state = jobs._state(jobs.load(running["job"], root=root))
    except KeyError:
        return False
    return state not in FINAL and running.get("pid", 0) > 0 and psutil.pid_exists(running["pid"])


def _room_gb() -> float:
    """Memory left before allocations fail: Windows' commit limit minus its commit charge; elsewhere, available RAM."""
    if os.name == "nt":
        import ctypes

        class Perf(ctypes.Structure):  # PERFORMANCE_INFORMATION; the commit figures count pages
            _fields_ = [("cb", ctypes.c_ulong), *[(n, ctypes.c_size_t) for n in (
                "commit_total", "commit_limit", "commit_peak", "physical_total", "physical_available", "system_cache",
                "kernel_total", "kernel_paged", "kernel_nonpaged", "page_size")],
                        *[(n, ctypes.c_ulong) for n in ("handles", "processes", "threads")]]
        p = Perf()
        p.cb = ctypes.sizeof(p)
        if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(p), p.cb):
            return (p.commit_limit - p.commit_total) * p.page_size / 2**30
    return psutil.virtual_memory().available / 2**30


def _max_runs() -> int:
    return min(settings.load()["videos_at_once"], PARALLEL + (_room_gb() >= ROOM_GB))


def _listed(state: dict) -> list[dict]:
    """The runs the tick state lists (a record from before parallel runs is one dict, not a list)."""
    r = state.get("running")
    return [r] if isinstance(r, dict) else r or []


def _runs(state: dict, root: Path) -> list[dict]:
    """The tick's jobs still running."""
    return [x for x in _listed(state) if _alive(x, root)]


def _end_run(x: dict) -> None:
    """End a listed run's process tree, if its PID still runs that job (spawn's argv names it). A PID that Windows has
    given to another process since the job ended is left alone."""
    try:
        if x.get("pid", 0) > 0 and x["job"] in psutil.Process(x["pid"]).cmdline():
            procs.kill_tree(x["pid"])
    except psutil.Error:  # ended already, or not ours to read or end
        pass


def _end_dropped(state: dict, root: Path) -> None:
    """End the run of every listed job that was dropped (the console, 9 Nov), before the tick lets it off its list
    (`dropped` is final): whether Drop's own stop_run answered busy or came too late to find the run listed, its kill
    is never lost."""
    dropped = set(_tick_jobs(root, states={"dropped"}))
    for x in _listed(state):
        if x.get("job") in dropped:
            _end_run(x)


def _run_ws(x: dict, root: Path) -> str:
    return x.get("ws") or jobs.load(x["job"], root=root).workspace


def _refused(root: Path, jid: str) -> list[str]:
    """Why the tick won't post this finished video; empty means clear. The upload guard's reasons (D4), plus a review
    block even when an operator's override lifts it there: an override goes out only by hand, private (controller
    ruling, 7 Nov). A verdict that can't be read refuses the video alone (fails closed), never the tick."""
    try:
        job = jobs.load(jid, root=root)
        why = guard(job)
        if _read(job.dir / "review.json").get("block") and "review blocked this video" not in why:
            why.append("review blocked this video; its override is for a hand upload, private")
        return why
    except Exception as e:
        return [f"{type(e).__name__}: {str(e)[:200]}"]


def _eligible(root: Path, ws: str, slot: datetime, report: dict | None = None) -> tuple[str, dict] | None:
    """The best finished video for this slot by topic score, among those the upload guard clears; nobody approves it
    (operator, 7 Nov). A refused one is never picked, and the report lists it with its reasons."""
    best = None
    for jid in _finished(root, ws):
        if why := _refused(root, jid):
            if report is not None:
                report.setdefault("refused", []).append({"job": jid, "reasons": why})
            continue
        score = float(_read(root / "jobs" / jid / "topic.json").get("score") or 0)
        if best is None or score > best[0]:
            best = (score, jid)
    return (best[1], {"kind": "auto", "slot": slot.isoformat()}) if best else None


def _capped(root: Path, pub: dict, platform: str, now: datetime) -> bool:
    """The channel has had its post on this platform today, in its timezone, or 2 in 24 h (5.4.8: caps count per
    platform), from the upload ledger."""
    with jobs._db(root) as c:
        mine = {j for (j,) in c.execute("select id from jobs where workspace = ?", (pub["ws"],))} - {pub["job"]}
    zone, rows =ZoneInfo(pub.get("tz", "UTC")), _rows(root / "uploads.jsonl", platform)
    times = [datetime.fromisoformat(r["at"]) for r in rows if r.get("job") in mine and r.get("at")]
    cap_day, cap_24h = CAP_DAY, CAP_24H
    try:
        ch_sc = load(pub["ws"], need=()).get("schedule", {})
        cap_day = int(ch_sc.get("cap_day", CAP_DAY))
        cap_24h = int(ch_sc.get("cap_24h", CAP_24H))
    except (ValueError, KeyError, TypeError):
        pass
    today = sum(t.astimezone(zone).date() == now.astimezone(zone).date() for t in times)
    return today >= cap_day or sum(now - t < timedelta(hours=24) for t in times) >= cap_24h


def _publish(state: dict, legs: dict, conn, root: Path, now: datetime, report: dict) -> None:
    """Every leg of the publishing video that isn't done, each on its own (Plan 3c Task 3): a failed leg retries on
    later ticks, up to PUBLISH_TRIES, never touching a finished one; a leg over its platform's cap, or whose token
    Meta refuses, closes at once. The console (9 Nov): Post anyway's record skips the caps, for that one publish; a
    video dropped while a leg waited, or while the leg before ran, posts nowhere more, its legs closed, as does a Reel
    leg that finds it dropped before media_publish (jobs.Dropped); a YouTube leg found landed (its publish stage done,
    upload.json written) counts, never run again."""
    pub = state["publishing"]
    pub.setdefault("legs", {"youtube": {"tries": pub.pop("tries", 0), "done": False}})  # a record from before the legs
    out = report.setdefault("publish", {"job": pub["job"]})
    for name, leg in pub["legs"].items():
        if leg["done"]:
            continue
        # asked before each leg (final review, 10 Nov): a drop can land while the one before runs, and the Reel leg
        # never passes through jobs.run, which stops a dropped job's YouTube leg
        if pub["job"] in _tick_jobs(root, pub["ws"], {"dropped"}):
            leg["done"], out[name] = True, "dropped"
            continue
        leg["tries"] += 1
        d = root / "jobs" / pub["job"]
        try:  # the cap reads the ledger: an unreadable line fails this leg alone, never the tick
            # a YouTube leg that landed before a cut (a console closed mid-post) counts as published; run again,
            # jobs.run would find every stage done, set the job back to done and land nothing (final review, 10 Nov)
            if not (name == "youtube" and (d / "publish.done.json").exists() and (d / "upload.json").exists()):
                if not pub.get("anyway") and _capped(root, pub, name, now):
                    leg["done"], out[name] = True, "capped"
                    continue
                legs[name](pub["job"])
        except instagram.TokenError as e:  # a refused token won't mend itself: no retries; this report says so
            leg["done"], out[name] = True, f"token refused: {str(e)[:200]}"
            continue
        except jobs.Dropped:  # dropped while the leg ran, before its step that can't be undone: nothing posted
            leg["done"], out[name] = True, "dropped"
            continue
        except Exception as e:  # a crash mid-publish: the next tick resumes this leg (each resumes its own session)
            out[name] = f"{type(e).__name__}: {str(e)[:200]}"
            if leg["tries"] >= PUBLISH_TRIES:
                leg["done"], out[name] = True, f"gave up: {out[name]}"
            continue
        leg["done"], out[name] = True, "published"
        if not pub.get("recorded"):  # the topic is published once, by whichever leg lands first
            pub["recorded"] = True
            topic = _read(root / "jobs" / pub["job"] / "topic.json")
            store.record_published(conn, pub["ws"], topic.get("key") or pub["job"], now)
            if topic.get("brief_id") is not None:
                store.mark(conn, topic["brief_id"], "published", now)
    if all(leg["done"] for leg in pub["legs"].values()):
        state.pop("publishing")
        _cleanup_job_media(root, pub["job"])


def _cleanup_job_media(root: Path, job_id: str) -> int:
    """Delete heavy rendered/source video files after a job finishes posting, keeping thumbnails and JSON records."""
    d = root / "jobs" / job_id
    if not d.is_dir():
        return 0
    try:
        from .console import read as console_read
        console_read.thumb(root, job_id)
    except Exception:
        pass
    removed = 0
    for name in ("final.mp4", "source_slice.mp4", "source.mp4"):
        f = d / name
        if f.is_file():
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    spec_path = d / "clip_spec.json"
    if spec_path.is_file():
        try:
            src = Path(str(json.loads(spec_path.read_text(encoding="utf-8")).get("source_path") or ""))
            if src.is_file() and (root / "clip_live") in src.resolve().parents and src.name.startswith("spike_"):
                src.unlink(missing_ok=True)
                removed += 1
        except (OSError, ValueError):
            pass
    return removed


def _prune_clip_caches(root: Path, *, source_max_age_h: float = 48.0, live_ts_max_age_h: float = 2.0) -> dict:
    """Prune stale VOD caches in clip_sources/ (>48h when no ingest is active) and orphaned seg_*.ts in clip_live/."""
    now_ts = time.time()
    pruned_sources = 0
    pruned_ts = 0

    # Skip clip_sources cleanup if any ingest is actively running
    active_ingest = False
    ing_path = root / "clip_ingests.json"
    if ing_path.is_file():
        try:
            data = json.loads(ing_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for entry in data.values():
                    if not isinstance(entry, dict) or entry.get("stage") in ("done", "error"):
                        continue
                    upd = entry.get("updated_at")
                    if upd:
                        age_s = now_ts - datetime.fromisoformat(str(upd).replace("Z", "+00:00")).timestamp()
                        if age_s < 1800:
                            active_ingest = True
                            break
        except (OSError, ValueError):
            pass

    src_dir = root / "clip_sources"
    if not active_ingest and src_dir.is_dir():
        cutoff = now_ts - source_max_age_h * 3600.0
        for f in src_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".mp4", ".m4a", ".webm", ".mkv", ".wav", ".mp3", ".ts", ".part"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                        pruned_sources += 1
                except OSError:
                    pass

    live_dir = root / "clip_live"
    if live_dir.is_dir():
        ts_cutoff = now_ts - live_ts_max_age_h * 3600.0
        for f in live_dir.rglob("seg_*.ts"):
            if f.is_file():
                try:
                    if f.stat().st_mtime < ts_cutoff:
                        f.unlink(missing_ok=True)
                        pruned_ts += 1
                except OSError:
                    pass

    return {"sources": pruned_sources, "live_ts": pruned_ts}


def _rank(ws: dict) -> dict:
    tp = ws.get("topics", {})
    return {"min_score": tp.get("min_score", 60), "half_life_days": tp.get("timely_half_life_days", 14)}


def _made_today(root: Path, ws: str, tz: str, now: datetime) -> int:
    """The tick's jobs for the channel made on this local date (schedule.max_per_day); a dropped one doesn't count
    (console, 9 Nov)."""
    zone = ZoneInfo(tz)
    with jobs._db(root) as c:
        stamps = dict(c.execute("select id, created_at from jobs where workspace = ? and state != 'dropped'",
                                (ws,)).fetchall())
    return sum(1 for j in _tick_jobs(root, ws) if stamps.get(j) and
               datetime.fromtimestamp(stamps[j], zone).date() == now.astimezone(zone).date())


def _retryable(root: Path, ws: str) -> str | None:
    """The newest failed or quarantined video the tick hasn't retried yet: one retry each, with a fresh budget, before
    anyone looks (operator, 9 Nov: no dead ends), up to settings' auto_retries. Settings that won't load allow none:
    the tick then serves nothing, and the console's board still shows its cards."""
    try:
        retries = settings.load()["auto_retries"]
    except ValueError:
        return None
    for jid in reversed(_tick_jobs(root, ws, {"failed", "quarantined"})):
        log = root / "jobs" / jid / "operator.jsonl"
        rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()] if log.exists() else []
        if sum(r.get("action") == "reset" and r.get("by") == "tick" for r in rows) < retries:
            return jid
    return None


def _busy(root: Path, ws: str) -> bool:
    """Something of this channel may still land: a video in progress, or one the tick will retry."""
    return bool(_tick_jobs(root, ws, IN_PROGRESS)) or _retryable(root, ws) is not None


def _fallbacks(root: Path, ws: str) -> list[tuple[float, str, dict]]:
    """Videos the review blocked for minor reasons only, best topic first: the last resort's candidates. A major block,
    or one with no severity, never is one."""
    with jobs._db(root) as c:
        at_review = {j for (j,) in c.execute("select id from jobs where state = 'quarantined' and stage = 'review'")}
    out = []
    for jid in _tick_jobs(root, ws, {"quarantined"}):
        d = root / "jobs" / jid
        v = _read(d / "review.json")
        if jid in at_review and v.get("block") and v.get("severity") == "minor" and (d / "final.mp4").exists():
            out.append((float(_read(d / "topic.json").get("score") or 0), jid, v))
    return sorted(out, key=lambda c: c[:2], reverse=True)


def _last_resort(root: Path, ws: str, slot: datetime, now: datetime, report: dict) -> tuple[str, dict] | None:
    """Nothing clean for the slot (operator, 9 Nov: "one video posted daily to each channel no matter what"): the best
    fallback is accepted on record once its prepublic checks pass, and posts if the upload guard then clears it."""
    from .prepublic import prepublic
    for _, jid, v in _fallbacks(root, ws):
        job = jobs.load(jid, root=root)
        try:
            pre = prepublic(job)  # off-limits words and near-duplicates still refuse it
        except jobs.Quarantined as e:
            report.setdefault("refused", []).append({"job": jid, "reasons": [str(e)[:200]]})
            continue
        at = now.isoformat()
        _write(job.dir / "review.json", {**v, "block": False, "last_resort": {"slot": slot.isoformat(),
                                                                               "blocked_for": v["reasons"]}})
        for name, kind, result in (("review", "creative", {"block": False, "last_resort": True,
                                                           "defects": v.get("defects", [])}),
                                   ("prepublic", "deterministic", pre)):
            _write(job.dir / f"{name}.done.json", {"stage": name, "kind": kind, "seconds": 0, "at": at, "result": result})
        jobs._set(job, "done", None)
        with (job.dir / "operator.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": at, "action": "last_resort", "by": "tick", "slot": slot.isoformat(),
                                "blocked_for": v["reasons"]}) + "\n")
        if why := _refused(root, jid):
            report.setdefault("refused", []).append({"job": jid, "reasons": why})
            continue
        report["last_resort"] = jid
        return jid, {"kind": "last_resort", "slot": slot.isoformat()}
    return None


def _uncovered(root: Path, ws: str, sc: dict, st: dict, now: datetime) -> datetime | None:
    """The channel's next slot no finished video covers (one still open counts), or None."""
    start = datetime.fromisoformat(st["slot_done"]) if st.get("slot_done") else now
    slots = slots_between(sc["slots"], sc["timezone"], start, now + HORIZON)
    fin = [j for j in _finished(root, ws) if not _refused(root, j)]
    return slots[len(fin)] if len(fin) < len(slots) else None


def _can_work(root: Path, conn, name: str, ws: dict, st: dict, now: datetime) -> bool:
    """The channel can move a video forward: one to resume or retry, or room for a new one today and a brief to make it
    from (or today's topic pass still to run)."""
    sc = ws["schedule"]
    if _busy(root, name):
        return True
    if sc.get("max_jobs") is not None and len(_tick_jobs(root, name)) >= sc["max_jobs"]:
        return False
    if sc.get("max_per_day") and _made_today(root, name, sc["timezone"], now) >= sc["max_per_day"]:
        return False
    return bool(store.live(conn, name, now, **_rank(ws))) or (
        bool(ws.get("topics")) and st.get("topics_day") != now.strftime("%Y%m%d"))


def _pick(active: list[tuple[str, dict]], state: dict, conn, root: Path, now: datetime) -> int:
    """Which channel this tick serves (operator, 9 Nov): a due slot with something to post first; then, with a run free,
    the channel with no job running whose next uncovered slot comes soonest and that can work on it; else the next in
    turn (its topic pass, its expiries, closing an open slot)."""
    turn = [(state.get("next", 0) + k) % len(active) for k in range(len(active))]
    chans = state.get("channels", {})
    for i in turn:
        name, ws = active[i]
        st = chans.get(name, {})
        if st.get("slot_done") and slots_between(ws["schedule"]["slots"], ws["schedule"]["timezone"],
                                                 datetime.fromisoformat(st["slot_done"]), now) and (
                any(not _refused(root, j) for j in _finished(root, name))
                or (_fallbacks(root, name) and not _busy(root, name))):
            return i
    runs = _runs(state, root)
    if len(runs) < _max_runs():
        taken = {_run_ws(x, root) for x in runs}
        urgent = [(when, k, i) for k, i in enumerate(turn) if active[i][0] not in taken
                  and (when := _uncovered(root, active[i][0], active[i][1]["schedule"], chans.get(active[i][0], {}), now))
                  and _can_work(root, conn, active[i][0], active[i][1], chans.get(active[i][0], {}), now)]
        if urgent:
            return min(urgent)[2]
    return turn[0]


def _channel(ws: dict, conn, state: dict, now: datetime, runner, legs: dict, root: Path, claude_bin, sensor,
             report: dict, may_publish: bool) -> None:
    name, sc, tp = ws["name"], ws["schedule"], ws.get("topics", {})
    st = state.setdefault("channels", {}).setdefault(name, {})
    st.setdefault("slot_done", now.isoformat())  # first sight: nothing is due until the channel's next slot
    rank = _rank(ws)
    due = slots_between(sc["slots"], sc["timezone"], datetime.fromisoformat(st["slot_done"]), now)
    if due and may_publish:  # the most recent due slot only; older ones were missed while asleep, never burst
        slot = due[-1]
        if len(due) > 1:
            report["missed_slots"] = len(due) - 1
        pick = _eligible(root, name, slot, report)
        if not pick and (now - slot >= LATE - timedelta(hours=1) or not _busy(root, name)):
            pick = _last_resort(root, name, slot, now, report)  # nothing clean, and nothing left that could land
        if pick:  # both platforms in the slot (8.5 item 22), each leg alone
            st["slot_done"] = slot.isoformat()
            _write(root / "jobs" / pick[0] / "approval.json", pick[1])
            todo, why = {"youtube": {"tries": 0, "done": False}}, instagram.off(ws)  # Reels follow the channel's mode
            if why:
                report["publish"] = {"job": pick[0], "instagram": why}
            else:  # a Reel already posted for this video (its YouTube leg gave up before) is never posted again
                todo["instagram"] = {"tries": 0, "done": (root / "jobs" / pick[0] / "instagram.json").exists()}
            state["publishing"] = {"job": pick[0], "ws": name, "tz": sc["timezone"], "legs": todo,
                                   "recorded": todo.get("instagram", {}).get("done", False)}
            _publish(state, legs, conn, root, now, report)
        elif now - slot >= LATE:
            st["slot_done"] = report["skipped_slot"] = slot.isoformat()
        else:  # the slot stays open: a video that lands late still posts for it (operator, 9 Nov)
            st["slot_done"], report["late_slot"] = (slot - timedelta(seconds=1)).isoformat(), slot.isoformat()
    _end_dropped(state, root)  # dropped while this tick ran: its run ends before the list lets it go
    runs = state["running"] = _runs(state, root)
    _auto_watch(ws, root, report)
    mine = [x for x in runs if _run_ws(x, root) == name]
    if mine or len(runs) >= _max_runs():  # one job per channel, two or three at once (operator, 9 Nov)
        report["running"] = (mine or runs)[0]["job"]
        report["expired"] = store.expire(conn, name, now, **rank)
        return
    waiting = _tick_jobs(root, name, IN_PROGRESS)
    fin = [j for j in _finished(root, name) if not _refused(root, j)]  # a refused video will never cover a slot
    need = len(slots_between(sc["slots"], sc["timezone"], now, now + HORIZON))
    if not waiting and len(fin) < need and (redo := _retryable(root, name)):  # no dead ends (operator, 9 Nov)
        with jobs._db(root) as c:
            stopped, step = c.execute("select state, stage from jobs where id = ?", (redo,)).fetchone()
        if stopped == "quarantined":  # its checks or review stopped it: the retry starts with a lesson (a crash doesn't)
            _write(root / "jobs" / redo / "lesson_pending.json", {"step": step, "at": now.isoformat()})
        jobs.reset(redo, budget=True, root=root, by="tick")
        waiting, report["retried"] = [redo], redo
    # schedule.max_jobs: the most videos the tick makes for this channel in all (operator, 6 Nov: "1 each for each
    # channel"); it still publishes them, and spends no topic pass it won't use. The operator raises it for more.
    cap, made = sc.get("max_jobs"), len(_tick_jobs(root, name))
    capped = cap is not None and made >= cap
    per_day = sc.get("max_per_day")  # schedule.max_per_day (operator, 6 Nov): the tick's jobs made on this local date
    today = _made_today(root, name, sc["timezone"], now) if per_day else 0
    if waiting:  # a job stopped by quota or a crash resumes before anything new (the soft cap stops production)
        state["running"].append({"job": waiting[0], "pid": runner(waiting[0]), "ws": name})
        report["resumed_job"] = waiting[0]
    else:
        if (capped or per_day and today >= per_day) and len(fin) < need:  # a full day still spends its topic pass
            report["capped"] = ({"channel": name, "made": today, "per_day": per_day} if per_day and today >= per_day
                                else {"channel": name, "made": made, "max_jobs": cap})
        elif len(fin) < need and (cands := store.live(conn, name, now, **rank)):
            brief = next((b for b in cands if b.get("lever") != st.get("last_lever")), cands[0])
            md = root / "briefs" / name / f"{brief['id']}.md"
            md.parent.mkdir(parents=True, exist_ok=True)
            md.write_text(f"# Brief: {ws['display_name']}: {brief['key']}\n\nMoment: {brief.get('moment', '')}\n\n"
                          f"Angle: {brief.get('angle', '')}\n\nReplay: {brief.get('replay', '')}\n\n"
                          f"Lever: {brief.get('lever', '')}\n", encoding="utf-8")
            jid = jobs.new(name, md, root=root)
            _write(root / "jobs" / jid / "topic.json", {"brief_id": brief["id"], "key": brief["key"],
                                                        "lever": brief.get("lever"), "score": brief.get("score")})
            store.mark(conn, brief["id"], "used", now)
            st["last_lever"] = brief.get("lever")
            state["running"].append({"job": jid, "pid": runner(jid), "ws": name})
            report["started"] = {"job": jid, "brief": brief["key"], "finished": len(fin), "slots_48h": need}
    # no topic pass beside this channel's own job, nor beside a full set of runs
    if (tp and not capped and not any(_run_ws(x, root) == name for x in state["running"])
            and len(state["running"]) < _max_runs() and st.get("topics_day") != now.strftime("%Y%m%d")):
        live = store.live(conn, name, now, **rank)
        if len(live) + len(fin) < sc.get("backlog_days", 14) * len(sc["slots"]):
            r = briefs.write_briefs(conn, ws, now, finished=len(fin), claude_bin=claude_bin, sensor=sensor, root=root)
            if not str(r["skipped"] or "").startswith("quota"):  # a pass refused for quota runs again later (9 Nov)
                st["topics_day"] = now.strftime("%Y%m%d")
            report["topics"] = {"queued": len(r["queued"]), "rejected": len(r["rejected"]), "skipped": r["skipped"]}
    report["expired"] = store.expire(conn, name, now, **rank)


def _scheduled(report: dict | None = None) -> list[tuple[str, dict]]:
    """Channels with a schedule. A folder without one (a voice-only stub) is not a channel the tick
    serves; only unreadable settings are reported."""
    out = []
    for name in channels():
        try:
            w = load(name, need=())
        except ValueError as e:
            if report is not None:
                report.setdefault("broken", []).append(f"{name}: {e}")
            continue
        if w.get("schedule"):
            out.append((name, w))
    return out


def tick(now: datetime | None = None, *, runner=None, uploader=None, reel=None, root: Path = OUT, claude_bin=None,
         sensor=None) -> dict:
    now = now or datetime.now(timezone.utc)
    runner = runner or (lambda jid: spawn(jid, root=root))
    legs = {"youtube": uploader or _upload, "instagram": reel or (lambda jid: _reel(jid, root))}
    with store.tick_lock(root) as held:
        if not held:
            return {"busy": True}
        report = {"at": now.isoformat(), "billing": os.environ.get("FACTORY_BILLING") or "subscription"}
        state_file = root / "tick_state.json"
        state = _read(state_file)
        _end_dropped(state, root)  # a kill Drop missed: first, before the publish phases that can hold the lock long
        try:
            _prune_clip_caches(root)
        except Exception:
            pass
        conn = store.connect(root)
        try:
            resumed = "publishing" in state
            if resumed:  # one video per tick: a crashed or failed leg goes first
                _publish(state, legs, conn, root, now, report)
            try:
                paused = settings.load()["paused"]
            except ValueError as e:  # a hand edit the console would have refused: nothing new runs until it loads
                report.setdefault("broken", []).append(f"factory settings: {e}")
                paused = True
            if paused:
                report["paused"] = True
            active = [] if paused else [(n, w) for n, w in _scheduled(report) if not w["schedule"].get("paused")]
            if active:
                i = _pick(active, state, conn, root, now)
                name, state["next"] = active[i][0], i + 1
                report["channel"] = name
                try:
                    ws = load(name)  # the full check, for the channel it serves
                except ValueError as e:
                    report.setdefault("broken", []).append(f"{name}: {e}")
                else:
                    _channel(ws, conn, state, now, runner, legs, root, claude_bin, sensor, report,
                             may_publish=not resumed)
        finally:
            conn.close()
            _write(state_file, state)
        with (root / "tick.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(report, default=str) + "\n")
        return report


@contextlib.contextmanager
def _lock(root: Path):
    """The tick lock for one console action: a tick holds it for seconds, so wait up to LOCK_WAIT_S, then Busy."""
    give_up = time.monotonic() + LOCK_WAIT_S
    while True:
        with store.tick_lock(root) as held:
            if held:
                yield
                return
        if time.monotonic() >= give_up:
            raise Busy(f"the tick lock stayed busy for {LOCK_WAIT_S} s")
        time.sleep(1)


def post_now(job_id: str, *, anyway: bool = False, approval: dict | None = None, root: Path = OUT,
             legs: dict | None = None, now: datetime | None = None) -> dict:
    """The console's Post now (9 Nov): one finished video, at once, through the tick's own publish routine (its legs,
    the upload guard, consent, ledger and Reel leg), holding the tick lock. A channel that has had its post today gets
    nothing posted and nothing written, and the video keeps its slot, unless `anyway`: Post anyway skips the caps, for
    this one publish. While another video's post is still finishing (its record in tick_state.json, a leg waiting for
    a retry), it raises Busy and writes nothing, as the tick starts no new publish then (controller ruling, 9 Nov). Its
    own record is written to tick_state.json before any leg runs, and taken off once every leg landed (final review,
    10 Nov): a console closed mid-post, or a leg left to retry, leaves the next tick that record to resume, as the tick
    resumes its own. Returns {published, publish (the legs' report, or None), capped}; raises Busy, NotReady (the
    guard's reasons, or not finished) or AlreadyPosted."""
    now = now or datetime.now(timezone.utc)
    legs = legs or {"youtube": _upload, "instagram": lambda jid: _reel(jid, root)}
    with _lock(root):
        job = jobs.load(job_id, root=root)
        ts = _read(root / "tick_state.json")
        if (other := ts.get("publishing", {}).get("job")) not in (None, job_id):
            raise Busy(f"another video's post is still finishing: {other}")
        if (job.dir / "upload.json").exists():  # under the lock: a second press waits for the first, then lands here
            raise AlreadyPosted(f"{job_id} is already posted")
        if reasons := _refused(root, job_id):
            raise NotReady("; ".join(reasons))
        if job_id not in _finished(root, job.workspace):
            raise NotReady("not finished")
        ws = load(job.workspace)
        pub = {"job": job_id, "ws": job.workspace, "tz": ws["schedule"]["timezone"], "anyway": anyway}
        if not anyway and _capped(root, pub, "youtube", now):
            return {"published": False, "publish": None, "capped": True}
        _write(job.dir / "approval.json", approval or {"kind": "console", "by": "operator", "at": now.isoformat()})
        todo, why = {"youtube": {"tries": 0, "done": False}}, instagram.off(ws)  # Reels follow the channel's mode
        report = {"publish": {"job": job_id, "instagram": why}} if why else {}
        if not why:  # a Reel already posted for this video is never posted again
            todo["instagram"] = {"tries": 0, "done": (job.dir / "instagram.json").exists()}
        state = {"publishing": {**pub, "legs": todo, "recorded": todo.get("instagram", {}).get("done", False)}}
        rest = {k: v for k, v in ts.items() if k != "publishing"}
        _write(root / "tick_state.json", {**rest, **state})  # first: a console closed mid-post leaves it to the tick
        conn = store.connect(root)
        try:
            _publish(state, legs, conn, root, now, report)
        finally:
            conn.close()
            _write(root / "tick_state.json", {**rest, **state})  # persist leg progress even if interrupted
        return {"published": report["publish"].get("youtube") == "published", "publish": report["publish"],
                "capped": False}


def run_now(job_id: str, *, root: Path = OUT, runner=None) -> dict:
    """The console's Run now: immediately spawn (or resume) a single job up to prepublic without waiting for the next
    tick, holding the tick lock and recording it in tick_state.json['running'] so the tick won't double-run it and
    Drop can stop it. If the job was stopped (failed/quarantined), resets it first with its regeneration budget
    restored (same as Retry)."""
    runner = runner or (lambda jid: spawn(jid, root=root))
    with _lock(root):
        job = jobs.load(job_id, root=root)
        state = jobs._state(job)
        if state == "dropped":
            raise NotReady(f"{job_id} is dropped")
        if (job.dir / "upload.json").exists() or (job.dir / "instagram.json").exists():
            raise AlreadyPosted(f"{job_id} is already posted")
        if state == "done" and (job.dir / "prepublic.done.json").exists():
            raise NotReady(f"{job_id} is already ready to post")
        ts = _read(root / "tick_state.json")
        runs = _runs(ts, root)
        if any(x.get("job") == job_id for x in runs):
            raise Busy(f"{job_id} is already running")
        if mine := [x for x in runs if _run_ws(x, root) == job.workspace]:
            raise Busy(f"{job.workspace} already has a job running ({mine[0]['job']})")
        limit = _max_runs()
        if len(runs) >= limit:
            raise Busy(f"already running {len(runs)} job(s) (limit {limit})")
        if state in ("failed", "quarantined"):
            jobs.reset(job_id, budget=True, root=root, by="console")
        pid = runner(job_id)
        runs.append({"job": job_id, "pid": pid, "ws": job.workspace})
        _write(root / "tick_state.json", {**ts, "running": runs})
        return {"ok": True, "job": job_id, "pid": pid}


def chain_next(finished_job_id: str, *, root: Path = OUT, runner=None) -> dict | None:
    """Auto-chain queue: right after a job finishes prepublic, prune it from tick_state.json['running'] and
    immediately start the next queued job on the same channel when auto_chain is enabled and concurrency limits allow."""
    runner = runner or (lambda jid: spawn(jid, root=root))
    with _lock(root):
        job = jobs.load(finished_job_id, root=root)
        ts = _read(root / "tick_state.json")
        runs = [x for x in _runs(ts, root) if x.get("job") != finished_job_id]
        try:
            ws = load(job.workspace, need=())
            fac_paused = settings.load().get("paused", False)
        except ValueError:
            _write(root / "tick_state.json", {**ts, "running": runs})
            return None
        sc = ws.get("schedule") or {}
        if fac_paused or sc.get("paused") or not sc.get("auto_chain", False):
            _write(root / "tick_state.json", {**ts, "running": runs})
            return None
        if any(_run_ws(x, root) == job.workspace for x in runs) or len(runs) >= _max_runs():
            _write(root / "tick_state.json", {**ts, "running": runs})
            return None
        waiting = [j for j in _tick_jobs(root, job.workspace, {"queued"}) if j != finished_job_id]
        if not waiting:
            _write(root / "tick_state.json", {**ts, "running": runs})
            return None
        nxt = waiting[0]
        pid = runner(nxt)
        runs.append({"job": nxt, "pid": pid, "ws": job.workspace})
        _write(root / "tick_state.json", {**ts, "running": runs})
        return {"job": nxt, "pid": pid}


def _auto_watch(ws: dict, root: Path, report: dict) -> None:
    """Auto-attach keyless Twitch & YouTube Live watchers for favorite streamers currently live on the configured game.

    Deduplicates by streamer name (preferring Twitch first when multistreaming) and probes
    https://www.youtube.com/@<handle>/live directly for remaining streamers so YouTube detection
    does not depend on search rankings or daily stream titles.
    """
    clip_cfg = ws.get("clip") or {}
    if (ws.get("format") or {}).get("pipeline") != "clip":
        return

    def _norm(val: str) -> str:
        return "".join(ch for ch in str(val or "").lower().lstrip("@") if ch.isalnum())

    raw_list = [str(x).strip().lstrip("@") for x in (clip_cfg.get("auto_watch_streamers") or []) if str(x).strip()]
    streamers = {_norm(x): x for x in raw_list if _norm(x)}
    if not streamers:
        return
    try:
        from .clip.live import LIVE_STATUS_FILE, twitch_login
        from .clip.radar import twitch_top_streams, youtube_channel_live
        from .console import act as console_act
    except Exception:
        return

    max_ready = int(clip_cfg.get("auto_watch_max_ready", 10))
    if max_ready > 0 and len(_finished(root, ws["name"])) >= max_ready:
        return

    def _norm_url(u: str) -> str:
        return str(u or "").strip().lower().replace("://www.", "://").rstrip("/")

    active_urls: set[str] = set()
    active_streamers: set[str] = set()
    active_count = 0
    now_ts = time.time()
    live_path = root / LIVE_STATUS_FILE
    if live_path.is_file():
        try:
            cur = json.loads(live_path.read_text(encoding="utf-8"))
            if isinstance(cur, dict):
                for entry in cur.values():
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("stage") not in ("starting", "watching"):
                        continue
                    pid_alive = bool(entry.get("pid") and psutil.pid_exists(int(entry["pid"])))
                    fresh = False
                    if upd := entry.get("updated_at"):
                        try:
                            fresh = (now_ts - datetime.fromisoformat(str(upd).replace("Z", "+00:00")).timestamp()) < 600
                        except ValueError:
                            pass
                    if not (pid_alive or fresh):
                        continue
                    active_count += 1
                    u = _norm_url(str(entry.get("url") or ""))
                    if u:
                        active_urls.add(u)
                        if tl := twitch_login(u):
                            active_streamers.add(_norm(tl))
                        for norm_key in streamers:
                            if norm_key in _norm(u) or norm_key in _norm(str(entry.get("detail") or "")):
                                active_streamers.add(norm_key)
        except (OSError, ValueError):
            pass

    max_watchers = int(clip_cfg.get("max_live_watchers", 2))
    if max_watchers > 0 and active_count >= max_watchers:
        return

    query = str(clip_cfg.get("auto_watch_query") or "").strip()
    raw_kw = clip_cfg.get("auto_watch_keywords")
    if isinstance(raw_kw, list) and raw_kw:
        yt_keywords = tuple(str(x).strip().lower() for x in raw_kw if str(x).strip())
    elif query:
        q_low = query.lower()
        yt_keywords = tuple(w for w in q_low.split() if len(w) >= 3) or (q_low,)
    else:
        yt_keywords = ()

    started = []

    # 1. Twitch first (1 fast GraphQL call; Twitch IRC is lighter and faster than YT live_chat)
    if query:
        try:
            for st in twitch_top_streams(game=query, limit=30):
                names = {_norm(st.get("channel") or ""), _norm(st.get("name") or "")}
                matched = names & set(streamers)
                url = str(st.get("url") or "").strip()
                if not matched or not url:
                    continue
                if (max_watchers > 0 and active_count >= max_watchers) or (matched & active_streamers) or _norm_url(url) in active_urls:
                    active_streamers.update(matched)
                    continue
                try:
                    res = console_act.start_live_watch(root, ws["name"], url, minutes=120.0)
                    active_urls.add(_norm_url(url))
                    active_streamers.update(matched)
                    active_count += 1
                    started.append({"channel": st.get("channel") or st.get("name"), "platform": "Twitch", "watch_id": res["watch_id"], "pid": res["pid"]})
                except Exception:
                    continue
        except Exception:
            pass

    # 2. Direct YouTube /@<handle>/live fallback for any remaining watchlist streamers
    for norm_key, raw_handle in streamers.items():
        if norm_key in active_streamers or (max_watchers > 0 and active_count >= max_watchers):
            continue
        st = youtube_channel_live(raw_handle)
        if not st:
            continue
        url = str(st.get("url") or "").strip()
        if not url or _norm_url(url) in active_urls:
            continue
        blob = " ".join([
            str(st.get("title") or ""),
            str(st.get("description") or ""),
            " ".join(str(t) for t in (st.get("tags") or [])),
        ]).lower()
        if yt_keywords and not any(kw in blob for kw in yt_keywords):
            continue
        try:
            res = console_act.start_live_watch(root, ws["name"], url, minutes=120.0)
            active_urls.add(_norm_url(url))
            active_streamers.add(norm_key)
            active_count += 1
            started.append({"channel": st.get("channel") or raw_handle, "platform": "YouTube", "watch_id": res["watch_id"], "pid": res["pid"]})
        except Exception:
            continue

    if started:
        report["auto_watch"] = started


def stop_run(job_id: str, *, root: Path = OUT) -> bool:
    """The console's Drop (9 Nov): when the tick state lists the job as running, end its process tree (_end_run) and
    take it off the list, holding the tick lock; False when it isn't listed. Raises Busy like post_now. A kill this
    misses, the tick makes up for (_end_dropped)."""
    with _lock(root):
        state = _read(root / "tick_state.json")
        runs = _listed(state)
        mine = [x for x in runs if x.get("job") == job_id]
        for x in mine:
            _end_run(x)
        if mine:
            _write(root / "tick_state.json", {**state, "running": [x for x in runs if x.get("job") != job_id]})
        return bool(mine)


def backlog(root: Path = OUT, now: datetime | None = None) -> dict:
    """For `backlog show`: per active channel, the live briefs, the finished videos and the next slots."""
    now, out, conn = now or datetime.now(timezone.utc), {}, store.connect(root)
    try:
        for name, ws in _scheduled():
            sc, tp = ws["schedule"], ws.get("topics", {})
            rank = {"min_score": tp.get("min_score", 60), "half_life_days": tp.get("timely_half_life_days", 14)}
            out[name] = {"paused": bool(sc.get("paused")),
                         "briefs": [{"score": b["effective_score"], "lever": b.get("lever"), "key": b["key"]}
                                    for b in store.live(conn, name, now, **rank)],
                         "finished": _finished(root, name),
                         "next_slots": [t.isoformat() for t in slots_between(sc["slots"], sc["timezone"], now,
                                                                             now + HORIZON)]}
    finally:
        conn.close()
    return out
