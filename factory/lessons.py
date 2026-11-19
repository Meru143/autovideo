"""The lessons loop (design D15 and D19, built on today's pipeline 9 Nov). When the tick retries a video its checks or
review stopped, the retry first runs a lessons call: it reads why the video stopped beside copies of the channel's
playbooks and fixes the gap in their text, so the retry, and every later video, starts from the corrected
instructions. No operator input (operator, 9 Nov: "i will have no input"): it learns from failures only. It edits only
the channel's declared playbooks, never review.md (the judge), settings, checks, the guard or code, and each lesson is
one commit with its reasons, undone by a revert (FAILURE_MODES.md, lessons)."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import ROOT
from .claude_call import SENSOR, QuotaWait, call
from .jobs import Job
from .workspace import load

PENDING = "lesson_pending.json"  # left by the tick when it retries a quarantined video
JUDGE = "review"  # the review playbook is a check: a lesson never sees or edits it
AUTHOR = "factory lessons <lessons@autovideo.invalid>"
SCHEMA = {"type": "object", "required": ["summary", "changes"], "properties": {
    "summary": {"type": "string"},
    "changes": {"type": "array", "items": {"type": "object", "required": ["file", "reason"], "properties": {
        "file": {"type": "string"}, "reason": {"type": "string"}}}}}}
PROMPT = """A video this channel was making stopped: its checks or its review rejected the work after its fix rounds.
evidence.json in your folder says which step stopped and why. The .md files beside it are copies of this channel's
instructions for its steps; the writer of each step reads its file before working.

Find the gap in these instructions that let this happen, and close it with the smallest edit that would have prevented
it: add or sharpen one rule where the writer of that step will read it, in the file's own voice and layout. Never loosen
a rule, never contradict a check, and keep every other rule as it is. If the cause isn't in the instructions (a crash,
a broken tool, a check that is plainly wrong), change nothing and say why in the summary.

Reply with JSON: summary (one line) and changes (one entry per file you edited, with the reason)."""


def skill_files(ws: dict) -> list[Path]:
    """The channel's declared playbooks a lesson may edit: all but the judge."""
    return [Path(ws["dir"]) / rel for name, rel in ws.get("playbooks", {}).items() if name != JUDGE]


def evidence(job: Job, step: str) -> dict:
    """Why the video stopped: its error, plus the stopped step's own findings where it keeps them."""
    d, ev = job.dir, {"channel": job.workspace, "job": job.id, "step": step}
    if (d / "error.txt").exists():
        ev["error"] = (d / "error.txt").read_text(encoding="utf-8", errors="replace")[:4000]
    if (d / "review_history.json").exists():
        ev["review"] = json.loads((d / "review_history.json").read_text(encoding="utf-8"))[-2:]
    found = sorted((d / "scene").glob("findings_*.json"), key=lambda p: p.stat().st_mtime) if (d / "scene").exists() else []
    if found:
        ev["scene_findings"] = json.loads(found[-1].read_text(encoding="utf-8"))
    for name in ("voice/voice.json", "prepublic.json"):
        if (d / name).exists():
            ev[Path(name).stem] = json.loads((d / name).read_text(encoding="utf-8"))
    return ev


def learn(job: Job, step: str, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR,
          repo: Path = ROOT) -> dict:
    """One lessons call on a stopped video; applies and commits the playbook edits it made. The record goes to
    <job>/lessons.jsonl: {at, step, summary, commit, files, ignored, skipped, error?}."""
    ws = load(job.workspace, need=())
    files = {f.name: f for f in skill_files(ws)}
    base = job.dir / "lessons"
    scratch = base / str(len(list(base.iterdir())) if base.exists() else 0)
    scratch.mkdir(parents=True)
    for f in files.values():
        shutil.copyfile(f, scratch / f.name)
    (scratch / "evidence.json").write_text(json.dumps(evidence(job, step), indent=1), encoding="utf-8")
    rec = {"at": datetime.now(timezone.utc).isoformat(), "step": step, "summary": "", "commit": None, "files": [],
           "ignored": [], "skipped": []}
    try:
        r = call("lessons", PROMPT, cwd=scratch, tools=("Read", "Write", "Edit"), schema=SCHEMA, claude_bin=claude_bin,
                 usage_log=job.dir / "usage.jsonl", sensor=sensor)
        if r.isolation:
            raise RuntimeError(f"lessons run not isolated, nothing applied: {r.isolation}")
        if r.data is None:
            raise RuntimeError("the lessons call gave no valid reply, nothing applied")
        rec["summary"] = str(r.data.get("summary", ""))[:200]
        why = {c.get("file"): c.get("reason", "") for c in r.data.get("changes", [])}
        changed = []
        for p in sorted(scratch.iterdir()):
            if p.name == "evidence.json" or not p.is_file():
                continue
            live = files.get(p.name)
            if live is None:
                rec["ignored"].append(p.name)  # the judge, the settings, a new file: never copied back
            elif p.read_bytes() != live.read_bytes():
                dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", str(live)],
                                       capture_output=True, text=True, timeout=60).stdout.strip()
                (rec["skipped"] if dirty else changed).append(p.name if dirty else (live, p))
        if changed:
            before = {live: live.read_bytes() for live, _ in changed}
            for live, p in changed:
                shutil.copyfile(p, live)
            body = "\n".join([f"job {job.id}, step {step}"] + [f"{live.name}: {why.get(live.name) or 'no reason given'}"
                                                                 for live, _ in changed])
            g = subprocess.run(["git", "-C", str(repo), "commit", "--author", AUTHOR, "-m",
                                f"lessons({job.workspace}): {rec['summary'] or 'a fix from a stopped video'}", "-m", body,
                                "--", *[str(live) for live, _ in changed]], capture_output=True, text=True, timeout=120)
            if g.returncode != 0:  # git busy or refusing: put the files back as they were
                for live, data in before.items():
                    live.write_bytes(data)
                raise RuntimeError(f"git commit failed, files restored: {(g.stderr or g.stdout)[-300:]}")
            rec["commit"] = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                           text=True, timeout=60).stdout.strip()
            rec["files"] = [live.name for live, _ in changed]
    except QuotaWait:
        raise  # the job waits for the reset, and the lesson runs again on resume
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    with (job.dir / "lessons.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def stage(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR, repo: Path = ROOT) -> dict:
    """The retry's first stage: the pending lesson, once. Whatever it finds, the retry goes ahead."""
    marker = job.dir / PENDING
    step = json.loads(marker.read_text(encoding="utf-8")).get("step") if marker.exists() else None
    rec = learn(job, step or "unknown", claude_bin=claude_bin, sensor=sensor, repo=repo) if marker.exists() else {}
    marker.unlink(missing_ok=True)
    return rec
