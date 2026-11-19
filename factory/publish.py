"""YouTube upload (design 5.8, 3.4, 5.5.3, 5.6; Plan 2b Task 11; FAILURE_MODES.md, publish). Deterministic code only:
Claude never uploads and never sees credentials. Every upload needs a dated workspace consent with uses left, is
resumable across crashes, and is confirmed through the API afterwards at the privacy it was sent with: public for the
tick's pick on a public channel, private otherwise."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import ROOT
from .jobs import Job
from .workspace import format_of, load


class ConsentMissing(RuntimeError):
    """No dated consent with uses left grants this upload."""


def public(ws: dict) -> bool:
    """A public channel (operator, 7 Nov: "upload directly"): schedule.mode "public" and a dated consent for scheduled
    public posts. The tick's picks there go up public, and Reels (always public) post only there (design 5.6)."""
    return ws.get("schedule", {}).get("mode") == "public" and any(
        c["action"] == "upload_public_scheduled" for c in ws["consent"])


class GuardRefused(RuntimeError):
    """The job is not fit to post: a stage unfinished, or the review missing or blocking (FAILURE_MODES.md, publish 13)."""


def guard(job: Job) -> list[str]:
    """Why this job must not post now; empty means clear. Hard rule, no human looks first (operator, 7 Nov)."""
    from .pipeline import PIPELINES  # late: pipeline imports this module
    ws = load(job.workspace)
    reasons = []
    for name, *_ in PIPELINES[format_of(ws)["pipeline"]]:
        if name == "publish":
            break
        if not (job.dir / f"{name}.done.json").exists():
            reasons.append(f"stage {name} is not done")
    pre = job.dir / "prepublic.json"
    if not pre.exists():
        reasons.append("no prepublic.json: the off-limits and near-duplicate checks never ran")
    elif not json.loads(pre.read_text(encoding="utf-8"))["ok"]:
        reasons.append("prepublic.json is not ok: off-limits words or a near-duplicate script")
    review = job.dir / "review.json"
    if not review.exists():
        reasons.append("no review.json: the review never ran")
    elif json.loads(review.read_text(encoding="utf-8"))["block"]:
        # the operator's dated consent naming this job lifts the block, nothing else (publish 14)
        if not any(c["action"] == "upload_private" and job.id in c.get("scope", "") for c in ws["consent"]):
            reasons.append("review blocked this video")
    return reasons


def _urls() -> tuple[str, str]:
    return (os.environ.get("FACTORY_YOUTUBE_UPLOAD_URL", "https://www.googleapis.com/upload/youtube/v3/videos"),
            os.environ.get("FACTORY_YOUTUBE_API_URL", "https://www.googleapis.com/youtube/v3/videos"))


def _rows(ledger: Path, platform: str = "youtube") -> list[dict]:
    """One platform's lines in the shared upload ledger; a line with no platform is YouTube's (from before Reels)."""
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()] if ledger.exists() else []
    return [r for r in rows if r.get("platform", "youtube") == platform]


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # whole or absent: a crash never leaves half a session to resume


def _consent(ws: dict, ledger: Path, job: Job) -> dict:
    """A consent with uses left. Each uploaded job counts once, and another job's open session counts too: it
    reserved its consent before its first byte."""
    used = {r.get("job") or f"line {i}": r["consent_id"] for i, r in enumerate(_rows(ledger))}
    for f in (job.root / "jobs").glob("*/upload_session.json"):
        if f.parent.name != job.id and f.parent.name not in used:
            used[f.parent.name] = json.loads(f.read_text(encoding="utf-8")).get("consent_id")
    if (job.dir / "approval.json").exists():  # the tick's pick: the channel's standing consent for its mode
        action = "upload_public_scheduled" if public(ws) else "upload_private_scheduled"
        if standing := [c for c in ws["consent"] if c["action"] == action]:
            return standing[0]
        raise ConsentMissing(f"{ws['name']} has no {action} consent for scheduled uploads in settings.json")
    spent = list(used.values())
    for c in ws["consent"]:
        if c["action"] == "upload_private" and spent.count(c.get("id")) < c.get("uses", 1):
            return c
    raise ConsentMissing(f"no consent with uses left for upload_private in {ws['name']}'s settings.json "
                         f"({len(spent)} upload(s) recorded or reserved); ask the operator and add a dated entry")


def _session(ws: dict):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    creds = Credentials.from_authorized_user_file(str(ROOT / ws["youtube_token"]))
    try:
        creds.refresh(Request())
    except RefreshError as e:  # never opens a login: the operator re-authorises on purpose
        raise RuntimeError(f"YouTube token refresh failed ({type(e).__name__}); re-run tools/youtube_auth.py") from None
    return AuthorizedSession(creds)


def upload_private(job: Job, video: Path, payload: dict, *, session=None, root: Path | None = None) -> dict:
    """The job's YouTube upload (named before channels went public). The upload decides the privacy (7 Nov): public
    for the tick's pick (approval.json) on a public channel, private for everything else; the metadata's own "private"
    is set to that, and any other value is refused."""
    if reasons := guard(job):
        raise GuardRefused(f"{job.id} will not upload: {'; '.join(reasons)}")
    root = root or job.root  # the factory's OUT in production; a test's own folder in the selftests
    ws, ledger = load(job.workspace), root / "uploads.jsonl"
    want = "public" if (job.dir / "approval.json").exists() and public(ws) else "private"
    if payload.get("status", {}).get("privacyStatus") not in ("private", want):
        raise ValueError(f"refusing the upload: this one goes up {want}, the payload asks {payload.get('status')}")
    if not video.is_file() or video.stat().st_size == 0:
        raise ValueError(f"no video to upload at {video}")
    done, state = job.dir / "upload.json", job.dir / "upload_session.json"
    size, sha = video.stat().st_size, hashlib.sha256(video.read_bytes()).hexdigest()
    prior = json.loads(done.read_text(encoding="utf-8")) if done.exists() else next(
        (r for r in _rows(ledger) if r.get("job") == job.id), None)
    sess = session or _session(ws)
    upload_url, api_url = _urls()
    if prior is not None:  # this job already uploaded: never a second upload or consent, only the privacy check again
        record = prior
        video_id = record["video_id"]
    else:
        offset, video_id = 0, None
        if state.exists():  # a crash after the session started: ask it how much arrived, then resume
            saved = json.loads(state.read_text(encoding="utf-8"))
            if saved.get("size") != size or saved.get("sha256", sha) != sha:
                raise RuntimeError(f"{video.name} changed since its upload session started ({saved.get('size')} -> "
                                   f"{size} bytes); delete {state.name} to upload the new file")
            uri = saved["uri"]
            consent_id = saved.get("consent_id") or _consent(ws, ledger, job).get("id")  # a session from before the rule
            probe = sess.put(uri, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"})
            if probe.status_code in (200, 201):
                video_id = probe.json()["id"]
            elif probe.status_code == 308:
                got = probe.headers.get("Range")  # e.g. "bytes=0-1048575"
                offset = int(got.split("-")[-1]) + 1 if got else 0
            else:
                raise RuntimeError(f"upload session probe returned {probe.status_code}: {probe.text[:200]}")
        else:
            consent_id = _consent(ws, ledger, job).get("id")
            start = sess.post(upload_url, params={"uploadType": "resumable", "part": "snippet,status"},
                              json={"snippet": payload["snippet"],
                                    "status": {**payload["status"], "privacyStatus": want}},
                              headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
            if start.status_code != 200 or "Location" not in start.headers:
                raise RuntimeError(f"could not start the upload: {start.status_code} {start.text[:200]}")
            uri = start.headers["Location"]
            _save(state, {"uri": uri, "size": size, "sha256": sha, "consent_id": consent_id,
                          "at": datetime.now(timezone.utc).isoformat()})  # before the first byte: it reserves the consent
        if video_id is None:
            with video.open("rb") as f:
                f.seek(offset)
                sent = sess.put(uri, data=f.read(), headers={"Content-Type": "video/mp4",
                                                             "Content-Range": f"bytes {offset}-{size - 1}/{size}"})
            if sent.status_code not in (200, 201):
                raise RuntimeError(f"upload failed: {sent.status_code} {sent.text[:200]}")
            video_id = sent.json()["id"]
        record = {"video_id": video_id, "consent_id": consent_id, "job": job.id,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if (job.dir / "approval.json").exists():  # the tick's pick record (kind auto, its slot)
            record["approval"] = json.loads((job.dir / "approval.json").read_text(encoding="utf-8"))
        _save(done, record)
        if not any(r.get("job") == job.id for r in _rows(ledger)):  # one line per job
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
    check = sess.get(api_url, params={"part": "status", "id": video_id})
    items = check.json().get("items", []) if check.status_code == 200 else []
    privacy = items[0]["status"]["privacyStatus"] if items else f"unknown ({check.status_code})"
    record = {**record, "privacy": privacy}
    _save(done, record)
    if privacy != want:
        raise RuntimeError(f"uploaded video {video_id} is {privacy}, not {want}: check YouTube Studio now")
    return {**record, "state": "uploaded_private"}
