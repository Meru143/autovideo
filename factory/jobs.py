"""Job store and resumable stage runner (design 5.5.1, 5.5.5; Plan 2b Task 2; FAILURE_MODES.md, jobs).

State lives in SQLite (OUT/factory.db); artifacts in OUT/jobs/<id>/. Each stage writes <stage>.done.json last, so a
re-run skips finished stages and repeats only the one that stopped. One regeneration budget of 2 per video is shared
by every creative retry (Plan 2b decision 6) and survives re-runs."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import OUT
from .claude_call import QuotaWait

MAX_REGENERATIONS = 2  # design L933, L1451, L1136: at most 2 regenerations per video, then the job stops
POSTING = "running:publish"  # the job's upload is running: a drop now is refused (final review, 10 Nov)


class Quarantined(RuntimeError):
    """A stage gives up on this video (out of regenerations, or a check that can't pass); artifacts are kept."""


class Posting(RuntimeError):
    """A drop refused: the video is being posted right now (its state is POSTING)."""


class Dropped(RuntimeError):
    """A post refused: the video was dropped before its step that can't be undone (posting)."""


def _db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "factory.db", timeout=30)
    conn.execute("create table if not exists jobs (id text primary key, workspace text not null, state text not null,"
                 " stage text, regenerations integer not null default 0, created_at real, updated_at real)")
    return conn


@dataclass
class Job:
    id: str
    workspace: str
    dir: Path
    root: Path

    @property
    def brief(self) -> Path:
        return self.dir / "brief.md"

    @property
    def regenerations(self) -> int:
        with _db(self.root) as c:
            return c.execute("select regenerations from jobs where id = ?", (self.id,)).fetchone()[0]

    def spend_regeneration(self) -> bool:
        """Take one regeneration from the video's budget; False when the budget is spent."""
        with _db(self.root) as c:
            took = c.execute("update jobs set regenerations = regenerations + 1, updated_at = ? "
                             "where id = ? and regenerations < ?", (time.time(), self.id, MAX_REGENERATIONS)).rowcount
        return took == 1

    @contextlib.contextmanager
    def regeneration(self, why: str | None):
        """Pays one regeneration for the call inside, raising Quarantined(why) when the budget is spent; a quota wall in
        that call gives it back, since nothing was tried. why=None is a first attempt: free, nothing to give back."""
        if why is not None and not self.spend_regeneration():
            raise Quarantined(why)
        try:
            yield
        except QuotaWait:
            if why is not None:
                with _db(self.root) as c:
                    c.execute("update jobs set regenerations = regenerations - 1, updated_at = ? "
                              "where id = ? and regenerations > 0", (time.time(), self.id))
            raise


Stage = tuple[str, Callable[[Job], dict], str]  # (name, fn, kind): kind is "creative" or "deterministic"


def new(workspace: str, brief: Path, *, root: Path = OUT) -> str:
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = root / "jobs" / job_id
    d.mkdir(parents=True)
    shutil.copyfile(brief, d / "brief.md")  # the job keeps the brief it started with
    with _db(root) as c:
        c.execute("insert into jobs values (?, ?, 'queued', null, 0, ?, ?)", (job_id, workspace, time.time(), time.time()))
    return job_id


def load(job_id: str, *, root: Path = OUT) -> Job:
    with _db(root) as c:
        row = c.execute("select workspace from jobs where id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"no job {job_id!r} in {root / 'factory.db'}")
    return Job(job_id, row[0], root / "jobs" / job_id, root)


def _set(job: Job, state: str, stage: str | None) -> bool:
    """False, changing nothing, when the job was dropped: a stage that ends late never moves a dropped job on (the
    console's Drop, 9 Nov). One statement, so a drop can't land between the check and the write."""
    with _db(job.root) as c:
        return c.execute("update jobs set state = ?, stage = ?, updated_at = ? where id = ? and state != 'dropped'",
                         (state, stage, time.time(), job.id)).rowcount == 1


def _state(job: Job) -> str:
    with _db(job.root) as c:
        return c.execute("select state from jobs where id = ?", (job.id,)).fetchone()[0]


def reset(job_id: str, *, budget: bool = False, root: Path = OUT, by: str = "operator") -> str:
    """The operator (or the tick's one retry, by="tick") reopens a job, logged in its operator.jsonl; budget=True also
    restores the regeneration budget."""
    job = load(job_id, root=root)
    with _db(root) as c:
        state, spent = c.execute("select state, regenerations from jobs where id = ?", (job_id,)).fetchone()
        c.execute("update jobs set state = 'queued', updated_at = ?" + (", regenerations = 0" if budget else "")
                  + " where id = ?", (time.time(), job_id))
    hist = job.dir / "review_history.json"
    if budget and hist.exists() and not (job.dir / "review.done.json").exists():
        # a fresh review cycle starts here: the review counts an owed repair's round from this point, so a job
        # blocked twice gets one more repair from its last block; the history stays whole (FAILURE_MODES review)
        start = len(json.loads(hist.read_text(encoding="utf-8")))
        (job.dir / "review_cycle.json").write_text(json.dumps({"start": start}), encoding="utf-8")
    with (job.dir / "operator.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "reset", "by": by, "from_state": state,
                            "regenerations_before": spent, "budget": budget}) + "\n")
    return "queued"


def drop(job_id: str, *, root: Path = OUT, by: str = "console") -> str:
    """The operator drops a video (the console, 9 Nov), logged in its operator.jsonl. Nothing moves a dropped job on:
    run() runs no stage of it, and the tick never resumes, retries, posts or counts it. Its files stay. Refused with
    Posting, writing nothing, while its upload runs (POSTING): that upload can't be called back, so a drop then would
    tell the owner "dropped" about a video going public (final review, 10 Nov). One statement, so the upload can't
    start between the check and the write."""
    job = load(job_id, root=root)
    with _db(root) as c:
        if not c.execute("update jobs set state = 'dropped', updated_at = ? where id = ? and state != ?",
                         (time.time(), job_id, POSTING)).rowcount:
            raise Posting(f"{job_id} is being posted right now: its upload can't be called back")
    with (job.dir / "operator.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "drop", "by": by}) + "\n")
    return "dropped"


@contextlib.contextmanager
def posting(job: Job):
    """Holds the job in POSTING across a post's step that can't be undone (the Reel's media_publish: final review,
    10 Nov), so drop() refuses meanwhile; a job already dropped raises Dropped and the step never runs (_set's own
    condition, one statement). The state from before comes back after. A cut inside leaves POSTING, which the tick
    clears as it resumes an unfinished job."""
    with _db(job.root) as c:
        state, stage = c.execute("select state, stage from jobs where id = ?", (job.id,)).fetchone()
    if not _set(job, POSTING, "publish"):
        raise Dropped(f"{job.id} was dropped: it posts nowhere more")
    try:
        yield
    finally:
        _set(job, state, stage)


def run(job_id: str, stages: list[Stage], *, root: Path = OUT) -> str:
    """Run every unfinished stage in order; return the job's state. A quarantined job runs nothing until `reset`; a
    dropped one runs nothing, and a stage that ends after the drop moves it on no further (console, 9 Nov)."""
    from .voice import QuotaExhausted
    job, result = load(job_id, root=root), {}
    # terminal (final review, Critical 1; a drop, console 9 Nov): no call, no new verdict, no upload
    if (state := _state(job)) in ("quarantined", "dropped"):
        return state
    for name, fn, kind in stages:
        done = job.dir / f"{name}.done.json"
        if done.exists():
            continue
        if not _set(job, f"running:{name}", name):
            return "dropped"
        t0 = time.time()
        try:
            result = fn(job) or {}
        except Quarantined as e:
            (job.dir / "error.txt").write_text(f"{name}: quarantined: {e}\n", encoding="utf-8")
            return "quarantined" if _set(job, "quarantined", name) else "dropped"
        except (QuotaExhausted, QuotaWait) as e:
            (job.dir / "error.txt").write_text(f"{name}: waiting for quota: {e}\n", encoding="utf-8")
            return "waiting_quota" if _set(job, "waiting_quota", name) else "dropped"
        except Exception:
            (job.dir / "error.txt").write_text(f"{name}:\n{traceback.format_exc()}", encoding="utf-8")
            return "failed" if _set(job, "failed", name) else "dropped"
        tmp = done.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stage": name, "kind": kind, "seconds": round(time.time() - t0, 2),
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "result": result}, indent=2, default=str),
                       encoding="utf-8")
        os.replace(tmp, done)  # a checkpoint is whole or absent
    state = result.get("state", "done")
    return state if _set(job, state, None) else "dropped"


def show(job_id: str, *, root: Path = OUT) -> dict:
    with _db(root) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute("select * from jobs where id = ?", (job_id,)).fetchone() or {})
    d = root / "jobs" / job_id
    row["done"] = sorted(p.name.removesuffix(".done.json") for p in d.glob("*.done.json"))
    return row
