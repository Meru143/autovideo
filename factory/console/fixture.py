"""The console's fixture (console Task 2): a scratch factory for the console's tests to serve, made with the factory's
own functions. dir/repo is a git repo of copies of every channel: a first commit by the repo's own identity, then a
lesson by "factory lessons" and a learning-loop change by "factory loop" with its record. dir/out is a factory root
holding a video in each place the console shows one (being made; stopped, the tick to retry it; stopped, needing the
owner; ready; ready on a channel that already posted today; on air with its views), a job folder with a half-written
review.json, a finished video the upload guard refuses, a hand-run job, queued briefs, the tick's last report and state, and a quota reading; dir/claude is the
Claude sign-in the health bar names. It names no channel: the jobs go to the channels by sorted name. The caller
points FACTORY_WORKSPACES at channels_root and FACTORY_CLAUDE_CONFIG at claude_config before serving."""
from __future__ import annotations

import gc
import json
import os
import shutil
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import jobs, publish, scheduler, stats
from ..instagram import _record
from ..lessons import AUTHOR as LESSONS
from ..loop.apply import AUTHOR as LOOP, _note
from ..pipeline import PIPELINES
from ..stages.metadata import payload
from ..topics import store
from ..workspace import channels, channels_root, format_of, load

OWNER = ("Owner", "owner@example.invalid")  # the scratch repo's own identity: the console's "you"
# name: (its channel's place in sorted order, hours since the tick made it, state, first stage not done, title)
PLAN = {"making": (0, 3, "running:music", "music", "The second knock"),
        "retry": (2, 30, "failed", "render", "Why the queue jumps"),
        "stuck": (1, 26, "quarantined", "review", "The yes that meant no"),
        "ready": (0, 20, "done", "publish", "The town that keeps one clock"),
        "ready2": (2, 28, "done", "publish", "The reply you rehearse"),
        "live": (2, 50, "done", "publish", "The pause that wins the room"),
        "refused": (1, 15, "done", "publish", "The fix that stuck"),
        "broken": (1, 10, "done", "publish", "Half a verdict"),
        "hand_run": (0, 72, "done", "publish", "A test cut")}
QUEUED = ("the call you let ring out", "a map drawn from memory", "the last seat on the bus")
NARRATION = "Nobody plans the pause. It just happens, and the whole room leans in to hear what comes next.\n"
BLOCKS = [{"block": True, "severity": "major", "reasons": ["the caption covers the speaker's face"], "defects": [],
           "fix_notes": ["move the caption above the face"]},
          {"block": True, "severity": "major", "reasons": ["the caption still covers the speaker's face"],
           "defects": [], "fix_notes": []}]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=60).stdout.strip()


MARKER = ".console-fixture"  # what build writes into its folder: the proof a later build may replace what is there


def _wipe(path: Path) -> None:
    """An earlier build's folder; git leaves its objects read-only, which Windows won't delete as they are."""
    if path.exists():
        gc.collect()  # an earlier build's sqlite connections (jobs._db's `with` doesn't close) still hold factory.db
        shutil.rmtree(path, onexc=lambda fn, p, _: (os.chmod(p, stat.S_IWRITE), fn(p)))


def _clip(dir: Path) -> Path:
    """Two seconds of test picture and tone (H.264, AAC, 9:16), and its first frame as a contact sheet beside it."""
    clip = dir / "clip.mp4"
    for args in (["-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=2", "-f", "lavfi", "-i",
                  "sine=frequency=330:sample_rate=48000:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a",
                  "aac", "-shortest", str(clip)], ["-i", str(clip), "-frames:v", "1", str(clip.with_suffix(".png"))]):
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True, timeout=120)
    return clip


class _Answer:
    """As much of a requests.Response as publish.upload_private reads."""

    def __init__(self, body: dict | None = None, **headers):
        self.status_code, self.body, self.headers, self.text = 200, body or {}, headers, ""

    def json(self) -> dict:
        return self.body


class _YouTube:
    """YouTube's resumable upload as publish.upload_private calls it: the session, the bytes, and a readback of the
    privacy it went up with. Nothing leaves the machine. ponytail: one upload per instance; a resumed session's
    readback isn't faked."""

    def __init__(self, jid: str):
        self.jid, self.privacy = jid, None

    def post(self, url, **kw):
        self.privacy = kw["json"]["status"]["privacyStatus"]
        return _Answer(Location=f"https://upload.invalid/{self.jid}")

    def put(self, uri, **kw):
        return _Answer({"id": f"yt-{self.jid[-6:]}"})

    def get(self, url, **kw):
        return _Answer({"items": [{"status": {"privacyStatus": self.privacy}}]})


def legs(root: Path) -> dict:
    """Fake publish legs for the tick's routine (scheduler.post_now, _publish). YouTube: the job's publish stage, run by
    jobs.run as the tick's _upload runs it, with publish.upload_private above a fake transport (the guard, the consent,
    the privacy, upload.json and the ledger line, all real). Instagram: publish_reel's record past its Graph calls
    (instagram.json, then one ledger line). Neither touches the network."""
    root = Path(root)

    def youtube(jid: str) -> str:
        def stage(job: jobs.Job) -> dict:
            body = json.loads((job.dir / "upload_payload.json").read_text(encoding="utf-8"))
            return publish.upload_private(job, job.dir / "final.mp4", body, session=_YouTube(job.id))
        state = jobs.run(jid, [("publish", stage, "deterministic")], root=root)
        if state != "uploaded_private":
            raise RuntimeError(f"publish ended {state}")
        return state

    def instagram(jid: str) -> dict:
        job = jobs.load(jid, root=root)
        consent = next(c for c in load(job.workspace)["consent"] if c["action"] == "upload_public_scheduled")
        record = {"platform": "instagram", "job": jid, "media_id": f"ig-{jid[-6:]}",
                  "permalink": f"https://www.instagram.com/reel/{jid[-6:]}/", "container_id": f"c-{jid[-6:]}",
                  "consent_id": consent.get("id"), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if (job.dir / "approval.json").exists():
            record["approval"] = json.loads((job.dir / "approval.json").read_text(encoding="utf-8"))
        return _record(job, root / "uploads.jsonl", job.dir / "instagram.json", record)
    return {"youtube": youtube, "instagram": instagram}


def _job(root: Path, conn, name: str, ws: dict, title: str, made: datetime, tick: bool) -> jobs.Job:
    """A job as the tick starts one (scheduler._channel): a queued brief, used; its brief file; topic.json. A hand-run
    job has neither the brief nor topic.json."""
    topics = ws.get("topics") or {}
    levers = topics.get("levers") or ["default"]
    lever = levers[0]
    brief = {"key": title.lower(), "moment": "m", "angle": "a", "replay": "r", "lever": lever, "timely": False,
             "score": 80}
    bid = store.queue_brief(conn, name, brief, made) if tick else "hand-run"
    md = root / "briefs" / name / f"{bid}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(f"# Brief: {ws['display_name']}: {brief['key']}\n\nLever: {lever}\n", encoding="utf-8")
    job = jobs.load(jobs.new(name, md, root=root), root=root)
    if tick:
        (job.dir / "topic.json").write_text(json.dumps({"brief_id": bid, "key": brief["key"], "lever": lever,
                                                        "score": brief["score"]}), encoding="utf-8")
        store.mark(conn, bid, "used", made)
    return job


def _through(job: jobs.Job, ws: dict, upto: str, made: datetime, title: str, clip: Path) -> None:
    """Every stage before `upto` done, each leaving what the console reads of it."""
    stages = PIPELINES[format_of(ws)["pipeline"]]
    order = [n for n, *_ in stages]
    done = order[:order.index(upto)]
    for k, (n, _, kind) in enumerate(stages[:len(done)]):  # jobs.run's checkpoint, its time naive and local
        at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(made.timestamp() + 600 * (k + 1)))
        (job.dir / f"{n}.done.json").write_text(json.dumps({"stage": n, "kind": kind, "seconds": 60.0, "at": at,
                                                            "result": {}}, indent=2), encoding="utf-8")
    (job.dir / "narration.txt").write_text(NARRATION, encoding="utf-8")
    if "render" in done:
        shutil.copyfile(clip, job.dir / "final.mp4")
        shutil.copyfile(clip.with_suffix(".png"), job.dir / "sheet.png")
    if "metadata" in done:
        meta = {"title": title, "description": NARRATION.strip(), "hashtags": ["#shorts"], "tags": [],
                "instagram_caption": f"{title}. #story #everyday #moments"}
        (job.dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        (job.dir / "upload_payload.json").write_text(json.dumps(payload(meta, ws), indent=2), encoding="utf-8")
    if "review" in done:
        verdict = {"block": False, "reasons": [], "defects": [], "fix_notes": []}
        (job.dir / "review.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        (job.dir / "review_history.json").write_text(json.dumps([verdict], indent=2), encoding="utf-8")
    if "prepublic" in done:
        (job.dir / "prepublic.json").write_text(json.dumps({"ok": True, "words": [], "similar": []}), encoding="utf-8")


def _unpause(path: Path) -> None:
    """The fixture is a known state, every channel live, whatever the live settings say: a channel paused on the
    factory branch would otherwise hide its ready cards' slots (10 Nov: a story channel paused for its engine rebuild)."""
    w = json.loads(path.read_text(encoding="utf-8-sig"))
    if w.get("schedule", {}).get("paused"):
        w["schedule"]["paused"] = False
        path.write_text(json.dumps(w, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build(dir: Path) -> dict:
    """Build the fixture afresh under dir; returns {root, repo, channels_root, jobs: {name: id}, claude_config}."""
    dir, src, now = Path(dir).resolve(), channels_root().resolve(), datetime.now(timezone.utc)
    if src.is_relative_to(dir):
        raise ValueError(f"the fixture would wipe the channels it copies: {src}")
    repo, root, claude = dir / "repo", dir / "out", dir / "claude"
    from .. import OUT  # read now: a launcher or a test may point it elsewhere
    live = Path(OUT).resolve()
    if live.is_relative_to(root.resolve()):  # out is the factory's, or holds it (one inside it, like a selftest's, is safe)
        raise ValueError(f"the fixture would wipe the factory's own out folder: {root}")
    if dir.exists() and any(dir.iterdir()) and not (dir / MARKER).exists():
        raise ValueError(f"{dir} is not empty and not a console fixture folder: pick a new or empty one")
    dir.mkdir(parents=True, exist_ok=True)
    (dir / MARKER).write_text("made by factory.console.fixture.build", encoding="utf-8")
    for p in (repo, root, claude):
        _wipe(p)
    repo.mkdir(parents=True)
    all_names = channels()  # sorted
    names = [n for n in all_names if "topics" in load(n, need=())] or all_names
    for n in names:
        shutil.copytree(src / n, repo / "channels" / n)
        _unpause(repo / "channels" / n / "settings.json")
    # FACTORY_SETTINGS points here. A known state whatever the operator set live (12 Nov: a paused factory failed the
    # pause test): running, and the values the browser tests read.
    s = json.loads((Path(__file__).resolve().parents[1] / "settings.json").read_text(encoding="utf-8-sig"))
    s.update(paused=False, videos_at_once=3, auto_retries=1, fallback={"model": "claude-opus-5-5", "effort": "high"})
    s["steps"].update(scene={"model": "claude-fable-5-2", "effort": "medium"},
                      script={"model": "claude-fable-5-2", "effort": "medium"},
                      research={"model": "claude-opus-5-5", "effort": "medium"})
    (repo / "factory-settings.json").write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", OWNER[0])
    _git(repo, "config", "user.email", OWNER[1])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "channels")
    saved, conn = os.environ.get("FACTORY_WORKSPACES"), None
    try:
        os.environ["FACTORY_WORKSPACES"] = str(repo / "channels")  # the factory's functions read the copies while building
        clip, conn, made = _clip(dir), store.connect(root), {}
        def last_slot(name: str) -> datetime:
            sc = load(name, need=())["schedule"]
            return scheduler.slots_between(sc["slots"], sc["timezone"], now - timedelta(days=2), now)[-1]
        for key, (place, hours, state, upto, title) in PLAN.items():
            name = names[place % len(names)]
            ws, at = load(name), now - timedelta(hours=hours)
            job = _job(root, conn, name, ws, title, at, tick=key != "hand_run")
            _through(job, ws, upto, at, title, clip)
            jobs._set(job, state, None if state == "done" else upto)
            with jobs._db(root) as c:
                c.execute("update jobs set created_at = ?, updated_at = ? where id = ?",
                          (at.timestamp(), at.timestamp() + 3600, job.id))
            made[key] = job.id
        d = {k: root / "jobs" / v for k, v in made.items()}
        (d["retry"] / "error.txt").write_text(
            "render:\nTraceback (most recent call last):\n  File \"factory/jobs.py\", line 140, in run\n"
            "RuntimeError: the renderer exited 1: Chrome lost its GPU context\n", encoding="utf-8")
        # stopped by its review twice, retried once by the tick, stopped again: the owner's now
        (d["stuck"] / "review.json").write_text(json.dumps(BLOCKS[-1], indent=2), encoding="utf-8")
        (d["stuck"] / "review_history.json").write_text(json.dumps(BLOCKS, indent=2), encoding="utf-8")
        jobs.reset(made["stuck"], budget=True, root=root, by="tick")
        jobs._set(jobs.load(made["stuck"], root=root), "quarantined", "review")
        (d["stuck"] / "error.txt").write_text("review: quarantined: the review blocked again after the repair round: "
                                              f"{BLOCKS[-1]['reasons']}\n", encoding="utf-8")
        (d["refused"] / "review.json").write_text(json.dumps(BLOCKS[0], indent=2), encoding="utf-8")  # finished, refused
        (d["broken"] / "review.json").write_text('{"block": false, "reasons": [', encoding="utf-8")  # cut off

        # the lesson the stuck video's retry made (lessons.learn's commit and record)
        lw = load(names[1 % len(names)], need=())
        book = Path(lw["dir"]) / lw["playbooks"]["music"]
        book.write_text(book.read_text(encoding="utf-8") + "\n- Keep the bed under the voice through every reveal.\n",
                        encoding="utf-8")
        _git(repo, "commit", "-q", "--author", LESSONS, "-m",
             f"lessons({names[1 % len(names)]}): the bed stays under the voice", "-m",
             f"job {made['stuck']}, step review\n{book.name}: the review heard the music over the reveal", "--",
             str(book))
        with (d["stuck"] / "lessons.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now.isoformat(), "step": "review",
                                "summary": "the bed stays under the voice", "commit": _git(repo, "rev-parse", "HEAD"),
                                "files": [book.name], "ignored": [], "skipped": []}) + "\n")
        # a learning-loop change (loop.apply.step's commit and record)
        pw = load(names[0], need=())
        book = Path(pw["dir"]) / pw["playbooks"]["topics"]
        p = {"target": "playbook", "path": "topics.md", "change": book.read_text(encoding="utf-8")
             + "\n- Lead with what the viewer stands to lose.\n", "rationale": "lead with the loss",
             "expected": "more viewers stay past the first beat"}
        b = {"verdict": "win", "wins": 3, "losses": 1, "seed": 7, "pairs": []}
        book.write_text(p["change"], encoding="utf-8")
        summary = f"loop({names[0]}): {p['rationale']}"
        _git(repo, "commit", "-q", "--author", LOOP, "-m", summary, "-m", json.dumps(
            {"change": {k: p[k] for k in ("target", "path", "expected")},
             "benchmark": {k: b[k] for k in ("wins", "losses", "seed")},
             "evidence": {"median": 41.0, "top": 52.5, "videos": 10}}, indent=1), "--", str(book))
        _note(root, names[0], {"at": now.isoformat(), "status": "applied",
                               "commit": _git(repo, "rev-parse", "HEAD"), "path": p["path"], "summary": summary,
                               "proposal": p, "benchmark": b})

        for n in names:  # the topic queue
            ws_cfg = load(n, need=())
            topics = ws_cfg.get("topics")
            if not topics:
                continue
            levers = topics.get("levers") or ["default"]
            for k, idea in enumerate(QUEUED):
                store.queue_brief(conn, n, {"key": idea, "moment": "m", "angle": "a", "replay": "r", "lever":
                                            levers[k % len(levers)], "timely": False, "score": 84 - 8 * k},
                                  now - timedelta(hours=k))
        # the tick: its state, its last report in tick.log as `python -m factory tick` prints it, the quota reading
        (root / "tick_state.json").write_text(json.dumps({"channels": {n: {
            "slot_done": last_slot(n).isoformat(), "topics_day": now.strftime("%Y%m%d")} for n in names},
            "next": 1}, indent=2), encoding="utf-8")
        (root / "tick.log").write_text(json.dumps({"at": now.isoformat(), "billing": "subscription", "channel":
                                                   names[0], "running": made["making"], "expired": 0}, indent=2)
                                       + "\n", encoding="utf-8")
        (root / "quota.json").write_text(json.dumps({"saved_at": now.isoformat(), "rate_limits": {
            "five_hour": {"used_percentage": 23.0, "resets_at": (now + timedelta(hours=3)).isoformat()},
            "seven_day": {"used_percentage": 41.0, "resets_at": (now + timedelta(days=4)).isoformat()}}}),
            encoding="utf-8")
        claude.mkdir(parents=True)
        (claude / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": OWNER[1]}}),
                                             encoding="utf-8")

        # on air: posted at its slot through the tick's own routine, so its channel has posted today; then its views
        live = names[PLAN["live"][0] % len(names)]
        scheduler.post_now(made["live"], approval={"kind": "auto", "slot": last_slot(live).isoformat()}, root=root,
                           legs=legs(root))
        stats.sync(conn, root)
        vid = json.loads((d["live"] / "upload.json").read_text(encoding="utf-8"))["video_id"]
        conn.execute("insert into video_stats (workspace, video_id, as_of, age_days, live_views, live_likes, "
                     "live_comments) values (?, ?, ?, 0, 1002, 31, 4)", (live, vid, now.date().isoformat()))
        # earlier days: the live video climbing, a second video that skips a day (a channel's total must not dip)
        for vid_, days in ((vid, ((2, 100), (1, 200))), ("yt-other", ((2, 400), (0, 450)))):
            for ago, views in days:
                conn.execute("insert into video_stats (workspace, video_id, as_of, age_days, live_views) values "
                             "(?, ?, ?, ?, ?)", (live, vid_, (now.date() - timedelta(days=ago)).isoformat(), 0, views))
        conn.commit()
    finally:
        if conn:
            conn.close()
        if saved is None:
            os.environ.pop("FACTORY_WORKSPACES", None)
        else:
            os.environ["FACTORY_WORKSPACES"] = saved
    return {"root": root, "repo": repo, "channels_root": repo / "channels", "jobs": made, "claude_config": claude,
            "settings": repo / "factory-settings.json"}
