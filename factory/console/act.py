"""The console's actions (console Task 3): every change the operator makes, each one calling factory code. Post now,
retry and drop are the scheduler's and jobs.py's own; settings and playbook edits are git commits (the repo's own
identity, `console(<channel>): <what>`), made only after the loader accepts them; an undo is `git revert`, taken back
by another when the loader refuses the settings it leaves. A refusal raises Conflict (409), Invalid (422) or NotFound
(404) before anything is written (an undo's 422, once its revert is taken back), and a failed edit or revert leaves
the file and the tree as they were. Run in the repo's folder as git sees it: `repo` is the repo holding the channels."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import jobs, scheduler, settings as factory_settings
from ..loop.apply import _note
from ..topics import store
from ..workspace import channels_root, load
from . import read

SCHEDULE = ("paused", "slots", "timezone", "max_per_day", "auto_chain")
TOPICS = ("levers", "min_score", "ideas_per_pass")
_edits = threading.Lock()  # ponytail: one git edit at a time, in this process; git's own index.lock covers the rest


class Conflict(Exception):
    """409: busy, already done, or the repo moved under the edit."""


class Invalid(Exception):
    """422: the change would not load."""


class NotFound(Exception):
    """404: no such id or name."""


def _job(root: Path, jid: str) -> jobs.Job:
    try:
        return jobs.load(jid, root=root)
    except KeyError:
        raise NotFound(f"no video {jid}") from None


def post(root: Path, jid: str, *, anyway: bool, legs: dict | None) -> dict:
    """Post now / Post anyway: the tick's own publish routine (scheduler.post_now) under the tick lock."""
    _job(root, jid)
    try:
        out = scheduler.post_now(jid, anyway=anyway, root=root, legs=legs)
    except (scheduler.Busy, scheduler.AlreadyPosted, scheduler.NotReady) as e:
        raise Conflict(str(e)) from None
    if out["capped"]:
        raise Conflict("Already posted today")
    return {"ok": out["published"], "publish": out["publish"]}


def run_job(root: Path, jid: str) -> dict:
    """Continue or start a job immediately without waiting for the next tick (scheduler.run_now under the tick lock)."""
    _job(root, jid)
    try:
        return scheduler.run_now(jid, root=root)
    except (scheduler.Busy, scheduler.AlreadyPosted, scheduler.NotReady) as e:
        raise Conflict(str(e)) from None


def retry(root: Path, jid: str) -> str:
    """Reopen a stopped video with its regeneration budget restored, as the tick's own retry does."""
    job = _job(root, jid)
    with jobs._db(root) as c:
        state = c.execute("select state from jobs where id = ?", (jid,)).fetchone()[0]
    if state not in ("failed", "quarantined"):
        raise Conflict(f"{jid} isn't stopped (it is {state})")
    return jobs.reset(job.id, budget=True, root=root, by="console")


def drop(root: Path, jid: str) -> str:
    """Drop first, then end a running process: a stage that finishes in between can't move a dropped job on. A posted
    video is refused, on YouTube or by a recorded Reel alone (final review, 10 Nov), and so is one being posted
    (jobs.drop's rule). A busy tick lock leaves the kill to the tick, which ends dropped runs itself."""
    job = _job(root, jid)
    if any((job.dir / f).exists() for f in ("upload.json", "instagram.json")):
        raise Conflict("It's already posted")
    try:
        state = jobs.drop(jid, root=root)
    except jobs.Posting:
        raise Conflict("It's being posted right now") from None
    try:
        scheduler.stop_run(jid, root=root)
    except scheduler.Busy:
        pass
    return state


def drop_queued(root: Path, *, channel: str | None = None) -> dict:
    """Drop all idle queued/stopped tick jobs (optionally for one channel) without touching actively running jobs."""
    ts = scheduler._read(root / "tick_state.json")
    active = {x["job"] for x in scheduler._runs(ts, root)}
    targets = scheduler._tick_jobs(root, channel, {"queued", "failed", "quarantined"})
    dropped: list[str] = []
    for jid in targets:
        if jid in active:
            continue
        job = jobs.load(jid, root=root)
        if any((job.dir / f).exists() for f in ("upload.json", "instagram.json")):
            continue
        try:
            jobs.drop(jid, root=root)
            dropped.append(jid)
        except jobs.Posting:
            continue
    return {"ok": True, "dropped": dropped, "count": len(dropped)}


def _head(repo: Path) -> str:
    return read._git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _put_back(path: Path, old: bytes) -> None:
    """Restore a file after a refused commit. Windows can refuse the replace while another process has the file open:
    retry once, then say so, naming the file (it holds the refused text, and running jobs read it)."""
    for attempt in (1, 2):
        try:
            return _write(path, old)
        except OSError as e:
            err = e
    raise Conflict(f"{path} couldn't be put back after git refused the commit ({err}): it holds the refused change, "
                   "so restore it with git checkout") from None


def _commit(repo: Path, path: Path, data: bytes, message: str, built_from: bytes | None = None) -> str:
    """Write the file and commit it alone; refuse while it has changes nobody committed, or when it is no longer the
    version `data` was built from (someone committed in between); undo the write if git says no."""
    old = path.read_bytes()
    if built_from is not None and old != built_from:
        raise Conflict(f"{path.name} changed while you were saving: reload and try again")
    if read._git(repo, "status", "--porcelain", "--", str(path)).stdout.strip():
        raise Conflict(f"{path.name} has changes that aren't committed")
    if data == old:
        raise Invalid("nothing changed")
    _write(path, data)
    r = read._git(repo, "commit", "-q", "-m", message, "--", str(path))
    if r.returncode:
        _put_back(path, old)
        raise Conflict(f"git refused the commit: {(r.stderr or r.stdout).strip()}")
    return _head(repo)


def _channel(name: str) -> dict:
    ws = read._channels().get(name)
    if ws is None:
        raise NotFound(f"no channel {name}")
    return ws


def save_settings(name: str, changes: dict, *, repo: Path) -> str:
    """A partial settings change, checked by workspace.load on a copy before it is written. Returns the commit."""
    path = Path(_channel(name)["dir"]) / "settings.json"
    with _edits:  # the read, the check and the commit are one step: nothing lands in between unseen
        return _save_settings(name, path, changes, repo)


def _save_settings(name: str, path: Path, changes: dict, repo: Path) -> str:
    raw = path.read_bytes()
    w = json.loads(raw.decode("utf-8-sig"))
    if not changes:
        raise Invalid("no change given")
    for k, v in changes.items():
        if k in SCHEDULE:
            if k in ("paused", "auto_chain") and type(v) is not bool:
                raise Invalid(f"{k} must be true or false")
            if v is None and k == "max_per_day":  # no cap
                w["schedule"].pop(k, None)
            else:
                w["schedule"][k] = v
        elif k == "instagram":
            if type(v) is not bool:
                raise Invalid("instagram must be true or false")
            w.setdefault("instagram", {})["enabled"] = v
        elif k == "topics" and isinstance(v, dict) and set(v) <= set(TOPICS):
            w.setdefault("topics", {}).update(v)
        elif k == "auto_watch_streamers" and isinstance(v, list) and all(isinstance(x, str) for x in v):
            w.setdefault("clip", {})["auto_watch_streamers"] = [x.strip().lower() for x in v if x.strip()]
        else:
            raise Invalid(f"can't change {k!r}: the settings take {', '.join(SCHEDULE)}, instagram and "
                          f"topics ({', '.join(TOPICS)})")
    out = (json.dumps(w, indent=2, ensure_ascii=False) + ("\n" if raw.endswith(b"\n") else "")).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:  # the loader reads <root>/<name>/settings.json
        (Path(tmp) / name).mkdir()
        (Path(tmp) / name / "settings.json").write_bytes(out)
        try:
            load(name, need=(), root=Path(tmp))  # the edit touches only what it checks even without the rest
        except ValueError as e:
            raise Invalid(str(e).partition("settings.json: ")[2] or str(e)) from None
    return _commit(repo, path, out, f"console({name}): {', '.join(changes)}", built_from=raw)


def save_factory_settings(changes: dict, *, repo: Path) -> str:
    """A partial change to factory/settings.json: top-level keys, with stop_at and steps merged one level deeper,
    checked by settings.check on the merged copy before it is written. Returns the commit."""
    path = factory_settings.path()
    if not changes:
        raise Invalid("no change given")
    with _edits:
        raw = path.read_bytes()
        try:
            w = json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:
            raise Invalid(f"{path.name} doesn't load: {e}") from None
        for k, v in changes.items():
            if k not in factory_settings.KEYS:
                raise Invalid(f"can't change {k!r}: the settings take {', '.join(factory_settings.KEYS)}")
            if k == "steps" and isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
                for step, c in v.items():
                    w["steps"][step] = {**w["steps"].get(step, {}), **c}
            elif k in ("stop_at", "fallback") and isinstance(v, dict):
                w[k] = {**w[k], **v}
            else:
                w[k] = v
        try:
            factory_settings.check(w)
        except ValueError as e:
            raise Invalid(str(e)) from None
        out = (json.dumps(w, indent=2, ensure_ascii=False) + ("\n" if raw.endswith(b"\n") else "")).encode("utf-8")
        return _commit(repo, path, out, f"console(factory): {', '.join(changes)}", built_from=raw)


def save_playbook(name: str, key: str, text: str, base: str, *, repo: Path) -> str:
    """Save a playbook, unless it changed since the editor loaded it (its last commit isn't `base`)."""
    ws = _channel(name)
    rel = ws.get("playbooks", {}).get(key)
    if rel is None:
        raise NotFound(f"no playbook {key} on {name}")
    path = Path(ws["dir"]) / rel
    if not isinstance(text, str) or not text.strip():
        raise Invalid("a playbook can't be empty")
    with _edits:
        last = read._git(repo, "log", "-1", "--format=%H", "--", str(path)).stdout.strip()
        if last != base:
            raise Conflict("the playbook changed since you opened it: reload it and edit again")
        data = text.replace("\r\n", "\n").encode("utf-8")
        if b"\r\n" in path.read_bytes():  # keep the file's own line ends: no whole-file diff
            data = data.replace(b"\n", b"\r\n")
        return _commit(repo, path, data, f"console({name}): edit {key} playbook")


def _abort_revert(repo: Path, sha: str) -> None:
    """Leave this undo's revert not half done: while REVERT_HEAD is `sha` (another revert in progress, the learning
    loop's rollback, isn't ours to abort), abort it, twice if need be, and check after each. A repo left with
    REVERT_HEAD and conflict markers in a live playbook is said so, naming the command."""
    for attempt in (1, 2, 3):
        if read._git(repo, "rev-parse", "-q", "--verify", "REVERT_HEAD").stdout.strip() != sha:
            return  # none in progress, or not this undo's
        if attempt < 3:
            read._git(repo, "revert", "--abort")
    raise Conflict("the undo failed and `git revert --abort` didn't finish: the repo needs attention, run "
                   "`git revert --abort` in it")


def _loads(repo: Path, files: list[str]) -> str | None:
    """The loader's refusal of a channel's settings.json or factory/settings.json among `files` (repo paths), as they
    are now; None when every one loads."""
    top = read._rel(repo, channels_root())
    factory_rel = read._rel(repo, factory_settings.path())
    for f in files:
        if f == factory_rel:
            try:
                factory_settings.load()
            except ValueError as e:
                return str(e)
            continue
        name = f.removeprefix(top + "/").partition("/")[0]
        if f == f"{top}/{name}/settings.json":
            try:
                load(name, need=())
            except ValueError as e:
                return str(e).partition("settings.json: ")[2] or str(e)
    return None


def undo(sha: str, *, repo: Path, root: Path) -> str:
    """git revert of one listed change. A conflict is aborted and refused. A revert that leaves a channel's settings the
    loader refuses (the loader changed since, an odd merge) is taken back by a revert of its own and refused with the
    loader's words, so no undo skips the settings check (final review, 10 Nov). A learning-loop change also gets a line
    in that loop's record. Returns the revert's commit."""
    found = next((c for c in read.changes(repo)["changes"] if c["sha"].startswith(sha)), None)
    if found is None:
        raise NotFound(f"no change {sha}")
    with _edits:
        r = read._git(repo, "revert", "--no-edit", found["sha"])
        if r.returncode:
            _abort_revert(repo, found["sha"])
            raise Conflict(f"can't undo this change: {(r.stderr or r.stdout).strip()}")
        done = _head(repo)
        if why := _loads(repo, found["files"]):
            back = read._git(repo, "revert", "--no-edit", done)  # this undo's own commit, whatever landed since
            if back.returncode:
                _abort_revert(repo, done)
                raise Conflict(f"the undo left settings the loader refuses ({why}), and git couldn't take it back: "
                               f"{(back.stderr or back.stdout).strip()}. Run `git revert {done}` in the repo")
            raise Invalid(why)
        if found["by"] == "loop" and found["channel"]:
            _note(root, found["channel"], {"at": datetime.now(timezone.utc).isoformat(), "status": "reverted",
                                           "by": "operator", "commit": found["sha"]})
        return done


def remove_topic(root: Path, brief_id: int) -> None:
    conn = store.connect(root)
    try:
        row = conn.execute("select status from briefs where id = ?", (brief_id,)).fetchone()
        if row is None:
            raise NotFound(f"no topic {brief_id}")
        if row["status"] != "queued":
            raise Conflict(f"topic {brief_id} is {row['status']}, not queued")
        store.mark(conn, brief_id, "removed")
    finally:
        conn.close()


def ingest_clip(root: Path, name: str, url: str, *, num_clips: int | None = None,
                layout: str | None = None, caption_style: str | None = None,
                provider: str | None = None, model: str | None = None, long_vod: bool | None = None) -> dict:
    """Start a detached background VOD ingest process so long streams survive closing the console and report live
    stage progress on the Console Home board."""
    import subprocess
    import sys
    import uuid
    from .. import ROOT
    from ..clip.pipeline import update_ingest_status
    ws = _channel(name)
    if ws.get("format", {}).get("pipeline") != "clip":
        raise Invalid(f"{name} is not a clip-pipeline channel")
    if not isinstance(url, str) or not url.strip():
        raise Invalid("a Twitch VOD URL, YouTube URL, or local file path is required")
    clean = url.strip()
    ingest_id = "ing-" + uuid.uuid4().hex[:8]
    update_ingest_status(ingest_id, channel=name, url=clean, stage="starting", detail="Starting background ingest", root=root)
    cmd = [sys.executable, "-m", "factory", "clip", "ingest", name, clean, "--ingest-id", ingest_id]
    if num_clips is not None:
        cmd += ["--num-clips", str(int(num_clips))]
    if layout:
        cmd += ["--layout", str(layout)]
    if caption_style:
        cmd += ["--caption-style", str(caption_style)]
    if provider:
        cmd += ["--provider", str(provider)]
    if model:
        cmd += ["--model", str(model)]
    if long_vod is True:
        cmd += ["--long-vod"]
    elif long_vod is False:
        cmd += ["--no-long-vod"]
    log = root / "clip_sources" / f"{ingest_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    base = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with log.open("ab") as out:
        for flags in ((base | 0x01000000, base) if os.name == "nt" else (base,)):
            try:
                pid = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                       env={**os.environ, "FACTORY_OUT": str(root)}, creationflags=flags).pid
                return {"ok": True, "ingest_id": ingest_id, "pid": pid, "queued": []}
            except OSError:
                continue
    update_ingest_status(ingest_id, channel=name, url=clean, stage="error", detail="Could not spawn ingest process",
                         error="OSError spawning ingest", root=root)
    raise Conflict("could not start background ingest process")


def clip_radar(query: str = "", *, mode: str = "all", limit: int = 12, hours: float = 24.0) -> dict:
    """Scan live Twitch/YouTube streams and fastest-rising YouTube videos (keyless)."""
    from ..clip.radar import scan_radar
    q = str(query or "").strip()
    if not q:
        raise Invalid("a game or topic query is required")
    m = mode if mode in ("streams", "trends", "all") else "all"
    lim = max(1, min(30, int(limit or 12)))
    hrs = max(1.0, min(168.0, float(hours or 24.0)))
    return scan_radar(query=q, mode=m, limit=lim, hours=hrs)


def start_live_watch(
    root: Path,
    name: str,
    url: str,
    *,
    minutes: float | None = None,
    max_height: int | None = None,
    ratio: float | None = None,
    min_msgs: int | None = None,
) -> dict:
    """Start a detached background live stream chat-spike watcher that survives closing the console."""
    import subprocess
    import sys
    import uuid
    from .. import ROOT
    from ..clip.live import update_live_status
    ws = _channel(name)
    if ws.get("format", {}).get("pipeline") != "clip":
        raise Invalid(f"{name} is not a clip-pipeline channel")
    if not isinstance(url, str) or not url.strip():
        raise Invalid("a live Twitch (https://twitch.tv/<login>) or YouTube live URL is required")
    clean = url.strip()
    watch_id = "live-" + uuid.uuid4().hex[:8]
    mins = max(5.0, min(720.0, float(minutes or 60.0)))
    update_live_status(watch_id, channel=name, url=clean, stage="starting", detail=f"Starting live watcher ({mins:g}m)", spikes=0, root=root)
    cmd = [
        sys.executable, "-m", "factory", "clip", "live", clean,
        "--channel", name, "--minutes", str(mins), "--watch-id", watch_id,
    ]
    if max_height is not None:
        cmd += ["--max-height", str(int(max_height))]
    if ratio is not None:
        cmd += ["--ratio", str(float(ratio))]
    if min_msgs is not None:
        cmd += ["--min-msgs", str(int(min_msgs))]
    log = root / "clip_live" / f"{watch_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    base = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with log.open("ab") as out:
        for flags in ((base | 0x01000000, base) if os.name == "nt" else (base,)):
            try:
                pid = subprocess.Popen(
                    cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    env={**os.environ, "FACTORY_OUT": str(root)}, creationflags=flags,
                ).pid
                update_live_status(watch_id, channel=name, url=clean, stage="starting", detail=f"Starting live watcher ({mins:g}m)", spikes=0, pid=pid, root=root)
                return {"ok": True, "watch_id": watch_id, "pid": pid}
            except OSError:
                continue
    update_live_status(watch_id, channel=name, url=clean, stage="error", detail="Could not spawn live watcher",
                       error="OSError spawning live watcher", root=root)
    raise Conflict("could not start background live watcher")


def stop_live_watch(root: Path, watch_id: str) -> dict:
    """Signal a running background live watcher to stop cleanly and flush pending spikes."""
    import re
    from ..clip.live import stop_flag_path, update_live_status
    if not watch_id or not re.fullmatch(r"[\w-]+", watch_id):
        raise Invalid("invalid watch_id")
    flag = stop_flag_path(watch_id, root=root)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stop\n", encoding="utf-8")
    update_live_status(watch_id, stage="stopping", detail="Stopping live watcher…", root=root)
    return {"ok": True, "watch_id": watch_id}


def dismiss_ingest(root: Path, ingest_id: str) -> dict:
    """Remove an ingest status card from OUT/clip_ingests.json under its OS lock."""
    import re
    from .. import locks
    from ..clip.pipeline import INGEST_STATUS_FILE, _atomic
    if not ingest_id or not re.fullmatch(r"[\w-]+", ingest_id):
        raise Invalid("invalid ingest_id")
    path = root / INGEST_STATUS_FILE
    with locks.held(root / "clip_ingests.lock", wait=True, poll_s=0.1):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or ingest_id not in data:
            raise NotFound(f"no ingest {ingest_id}")
        data.pop(ingest_id, None)
        _atomic(path, data)
    return {"ok": True, "ingest_id": ingest_id}


def retry_ingest(root: Path, ingest_id: str) -> dict:
    """Retry a failed VOD ingest using its saved channel and URL (reuses cached audio on disk)."""
    import re
    from .. import locks
    from ..clip.pipeline import INGEST_STATUS_FILE, _atomic
    if not ingest_id or not re.fullmatch(r"[\w-]+", ingest_id):
        raise Invalid("invalid ingest_id")
    path = root / INGEST_STATUS_FILE
    with locks.held(root / "clip_ingests.lock", wait=True, poll_s=0.1):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        entry = data.get(ingest_id) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            raise NotFound(f"no ingest {ingest_id}")
        channel = str(entry.get("channel") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not channel or not url:
            raise Invalid("ingest record is missing channel or url")
        data.pop(ingest_id, None)
        _atomic(path, data)
    return ingest_clip(root, channel, url)


def dismiss_live_watch(root: Path, watch_id: str) -> dict:
    """Remove a finished, stopped, or errored live watcher card from OUT/clip_live_watchers.json."""
    import re
    from .. import locks
    from ..clip.live import LIVE_STATUS_FILE, _atomic_live
    if not watch_id or not re.fullmatch(r"[\w-]+", watch_id):
        raise Invalid("invalid watch_id")
    path = root / LIVE_STATUS_FILE
    with locks.held(root / "clip_live.lock", wait=True, poll_s=0.1):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or watch_id not in data:
            raise NotFound(f"no live watcher {watch_id}")
        data.pop(watch_id, None)
        _atomic_live(path, data)
    return {"ok": True, "watch_id": watch_id}


def save_clip(root: Path, jid: str, body: dict) -> dict:
    """Validate and atomically update a clip job's clip_spec.json."""
    from ..clip import CAPTION_STYLES, LAYOUTS, PACINGS
    job = _job(root, jid)
    spec_path = job.dir / "clip_spec.json"
    if not spec_path.is_file():
        raise NotFound(f"{jid} has no clip_spec.json")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if "start_s" in body:
        spec["start_s"] = round(float(body["start_s"]), 2)
    if "end_s" in body:
        spec["end_s"] = round(float(body["end_s"]), 2)
    if spec["end_s"] <= spec["start_s"]:
        raise Invalid("end_s must be greater than start_s")
    if "layout" in body:
        if body["layout"] not in LAYOUTS:
            raise Invalid(f"layout must be one of {', '.join(LAYOUTS)}")
        spec["layout"] = body["layout"]
    if "caption_style" in body:
        if body["caption_style"] not in CAPTION_STYLES:
            raise Invalid(f"caption_style must be one of {', '.join(CAPTION_STYLES)}")
        spec["caption_style"] = body["caption_style"]
    if "pacing" in body:
        if body["pacing"] not in PACINGS:
            raise Invalid(f"pacing must be one of {', '.join(PACINGS)}")
        spec["pacing"] = body["pacing"]
    if "include_captions" in body:
        spec["include_captions"] = bool(body["include_captions"])
    if "speed" in body:
        spd = float(body["speed"])
        if not 0.5 <= spd <= 2.0:
            raise Invalid("speed must be between 0.5 and 2.0")
        spec["speed"] = spd
    if "captions" in body and isinstance(body["captions"], list):
        spec["captions"] = body["captions"]
    _write(spec_path, json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8"))
    return {"ok": True, "spec": spec}


def save_metadata(root: Path, jid: str, body: dict) -> dict:
    """Atomically update a job's title, YouTube description, Instagram caption, and pinned comment."""
    job = _job(root, jid)
    with jobs._db(root) as c:
        st = c.execute("select state from jobs where id = ?", (jid,)).fetchone()
    if st and st[0] == jobs.POSTING:
        raise Invalid("cannot edit metadata while the video is actively posting")
    meta_path = job.dir / "metadata.json"
    if not meta_path.is_file():
        raise NotFound(f"{jid} has no metadata.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    title = meta.get("title", "")
    if "title" in body:
        title = str(body["title"] or "").strip()
        if not title:
            raise Invalid("title cannot be empty")
        if len(title) > 100:
            raise Invalid("title must be 100 characters or fewer")
        meta["title"] = title
    description = meta.get("description", "")
    if "description" in body:
        description = str(body["description"] or "").strip()
        meta["description"] = description
    pinned_comment = meta.get("pinned_comment", "")
    if "pinned_comment" in body:
        pinned_comment = str(body["pinned_comment"] or "").strip()
        meta["pinned_comment"] = pinned_comment
    _write(meta_path, json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))

    payload_path = job.dir / "upload_payload.json"
    if payload_path.is_file():
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        snippet = payload.setdefault("snippet", {})
        if "title" in body:
            snippet["title"] = title
        if "description" in body:
            snippet["description"] = description
        _write(payload_path, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))

    ig_path = job.dir / "instagram_caption.txt"
    if "instagram_caption" in body:
        ig_cap = str(body["instagram_caption"] or "").strip()
        _write(ig_path, ig_cap.encode("utf-8"))
    else:
        ig_cap = ig_path.read_text(encoding="utf-8") if ig_path.is_file() else ""

    return {"ok": True, "metadata": {
        "title": title, "description": description, "instagram_caption": ig_cap, "pinned_comment": pinned_comment,
    }}


def approve_nsfw(root: Path, jid: str) -> dict:
    """Write nsfw_override.json for a clip flagged by prepublic NSFW text checks and retry it."""
    job = _job(root, jid)
    override = {"approved": True, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _write(job.dir / "nsfw_override.json", json.dumps(override, indent=2).encode("utf-8"))
    state = retry(root, jid)
    return {"ok": True, "state": state}


def rerender_clip(root: Path, jid: str) -> dict:
    """Re-run the clip renderer in place after Clip Editor tweaks."""
    from ..clip.pipeline import clip_cut
    job = _job(root, jid)
    if not (job.dir / "clip_spec.json").is_file():
        raise NotFound(f"{jid} has no clip_spec.json")
    info = clip_cut(job)
    return {"ok": True, "render": info}


def save_providers(changes: dict) -> dict:
    """Atomically update multi-provider LLM routing in secrets/providers.json without touching secret keys."""
    from .. import ROOT
    if not isinstance(changes, dict) or not changes:
        raise Invalid("no provider changes given")
    cfg_path = ROOT / "secrets" / "providers.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with _edits:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
        except ValueError as e:
            raise Invalid(f"providers.json doesn't load: {e}") from None
        if not isinstance(cfg, dict):
            cfg = {}
        if "active_provider" in changes:
            ap = str(changes["active_provider"] or "").strip()
            if not ap:
                raise Invalid("active_provider cannot be empty")
            cfg["active_provider"] = ap
        if "fallback_providers" in changes:
            fb = changes["fallback_providers"]
            if not isinstance(fb, list) or not all(isinstance(x, str) and x.strip() for x in fb):
                raise Invalid("fallback_providers must be a list of provider names")
            cfg["fallback_providers"] = [x.strip() for x in fb]
        if "steps" in changes:
            st = changes["steps"]
            if not isinstance(st, dict):
                raise Invalid("steps must be an object mapping step -> provider")
            cur = dict(cfg.get("steps") or {})
            for step, prov in st.items():
                val = str(prov or "").strip()
                if val:
                    cur[step] = val
                else:
                    cur.pop(step, None)
            cfg["steps"] = cur
        if "models" in changes:
            ms = changes["models"]
            if not isinstance(ms, dict):
                raise Invalid("models must be an object mapping provider -> model")
            for prov, model in ms.items():
                if prov in ("active_provider", "fallback_providers", "steps"):
                    continue
                spec = dict(cfg.get(prov) or {}) if isinstance(cfg.get(prov), dict) else {}
                val = str(model or "").strip()
                if val:
                    spec["model"] = val
                else:
                    spec.pop("model", None)
                cfg[prov] = spec
        _write(cfg_path, (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return {"ok": True, "llm": read._llm_providers()}
