"""Instagram Reels by resumable upload (Plan 3c Task 2; design 5.4.8, 8.5 item 22; FAILURE_MODES.md, instagram). The
Content Publishing API with the channel's token: a REELS container, the local file's bytes to rupload (no media host),
status polling, media_publish and the permalink. Idempotent per job like the YouTube upload: a session before the
first byte, a record after the publish, one ledger line per job and platform. Nothing posts from a private channel,
and no error text carries the token."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import ROOT, jobs, publish
from .jobs import Job
from .publish import ConsentMissing, _rows, _save
from .workspace import instagram_of, load

ENV = ROOT / "secrets" / "meta.env"
GRAPH, RUPLOAD = "https://graph.facebook.com/v21.0", "https://rupload.facebook.com/ig-api-upload/v21.0"
MAX_BPS = 25_000_000  # Reels' video bitrate cap (IG User Media reference: "VBR, 25Mbps maximum")


def _env(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return {k.strip(): v.strip() for k, _, v in (ln.partition("=") for ln in lines) if v.strip() and not k.startswith("#")}


class InstagramError(RuntimeError):
    """A Graph API call failed, or Reels won't take the file. The message never carries the token."""


class PublishLimit(InstagramError):
    """Meta's 24-hour publishing limit is reached: the Reel waits and never publishes into it."""


class TokenError(InstagramError):
    """Meta refused the channel's token (OAuthException 190, or 10 or 200-299 for a permission), or it is missing. The
    tokens never expire (system-user tokens, 6 Nov), so a person re-creates it: the leg stops instead of retrying."""


class Graph:
    """One channel's Instagram account: IG_USER_ID_<CHANNEL> and IG_TOKEN_<CHANNEL> from secrets/meta.env."""

    def __init__(self, channel: str, *, base: str | None = None, upload: str | None = None, env: Path = ENV):
        vals, key = _env(env), channel.upper()
        self.user, self.token = vals.get(f"IG_USER_ID_{key}", ""), vals.get(f"IG_TOKEN_{key}", "")
        self.base = (base or os.environ.get("FACTORY_GRAPH_URL") or GRAPH).rstrip("/")
        self.upload = (upload or os.environ.get("FACTORY_RUPLOAD_URL") or RUPLOAD).rstrip("/")
        if not (self.user and self.token):
            raise TokenError(f"no IG_USER_ID_{key} and IG_TOKEN_{key} in {env.name}")

    def call(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 120, **params) -> dict:
        """One request: a Graph path, or a full rupload URL with its body. Its JSON, or InstagramError with Meta's
        message and code."""
        url = path if path.startswith("http") else f"{self.base}/{path}"
        if body is None:  # a Graph call: the token rides in the query (GET) or the form (POST)
            form = urllib.parse.urlencode({**params, "access_token": self.token})
            url, body = (f"{url}?{form}", None) if method == "GET" else (url, form.encode())
        try:  # the request is built in here too: urllib's "unknown url type" error quotes the URL, token and all
            req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:  # the Graph says "error"; rupload says "debug_info" (7 Nov)
                j = json.loads(e.read() or b"{}")
                err = j.get("error") or j.get("debug_info") or {}
            except (ValueError, AttributeError):
                err = {}
            code = err.get("code")  # by code, not type: a passing fault (code 2) is typed OAuthException too
            kind = TokenError if code in (190, 10) or (type(code) is int and 200 <= code <= 299) else InstagramError
            raise kind(self._clean(f"{method} {path}: {e.code} {err.get('message', '')} (code {code})")) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise InstagramError(self._clean(f"{method} {path}: {e}")) from None

    def _clean(self, text: str) -> str:
        return text.replace(self.token, "<token>")


def off(ws: dict) -> str | None:
    """Why the channel's Reels are off, or None when they post. Reels are always public, so they follow the channel's
    YouTube mode (design 5.6): only a public channel, with its dated public consent, posts them."""
    if not publish.public(ws):
        return "off (a private channel)"
    if not instagram_of(ws)["enabled"]:
        return "off (instagram.enabled is false)"
    return None


def _int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _bitrate_bps(video: Path, stream_bps, format_bps=None, duration_s: float = 0.0) -> int:
    """Video bitrate in bps: stream bit_rate when numeric, else format bit_rate, else file_size * 8 / duration_s.
    Returns MAX_BPS + 1 (fail closed) if neither bitrate nor duration can be determined."""
    if (bps := _int(stream_bps)) > 0:
        return bps
    if (bps := _int(format_bps)) > 0:
        return bps
    if duration_s > 0:
        return int(video.stat().st_size * 8 / duration_s)
    return MAX_BPS + 1


def preflight(video: Path) -> list[str]:
    """What Reels won't take (Plan 3c Review Focus 4): 9:16, H.264 and AAC, 23-60 fps, at most 25 Mbps of video,
    under 1 GB, 3 s to 15 min."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,"
                        "avg_frame_rate,bit_rate:format=duration,bit_rate", "-of", "json", str(video)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        return [f"ffprobe can't read {video.name}: {r.stderr.strip()[-200:]}"]
    info = json.loads(r.stdout)
    v, a = (next((s for s in info.get("streams", []) if s.get("codec_type") == t), {}) for t in ("video", "audio"))
    num, _, den = (v.get("avg_frame_rate") or "0/0").partition("/")
    fps = int(num) / int(den) if den.isdigit() and int(den) else 0.0
    w, h, size = v.get("width") or 0, v.get("height") or 1, video.stat().st_size
    fmt = info.get("format", {})
    secs = _float(fmt.get("duration"))
    bps = _bitrate_bps(video, v.get("bit_rate"), fmt.get("bit_rate"), secs)
    return [why for bad, why in ((v.get("codec_name") != "h264", f"video is {v.get('codec_name')}, not H.264"),
                                 (a.get("codec_name") != "aac", f"audio is {a.get('codec_name')}, not AAC"),
                                 (abs(w / h - 9 / 16) > 0.01, f"{w}x{h} is not 9:16"),
                                 (not 23 <= fps <= 60, f"{fps:.1f} fps is outside 23-60"),
                                 (bps > MAX_BPS, f"video at {bps / 1e6:.1f} Mbps is over Reels' 25 Mbps"),
                                 (size >= 1 << 30, f"{size} bytes is 1 GB or more"),
                                 (not 3 <= secs <= 900, f"{secs:.1f} s is outside 3 s to 15 min")) if bad]


def fit(video: Path) -> Path:
    """The file a Reel posts: the video, or reel.mp4 beside it when its video is over Reels' bitrate cap (a story
    channel's canvas film ran at 26 Mbps, 7 Nov). The copy is made once per version of the video, so a resumed session
    finds the same bytes; YouTube keeps the full file."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=bit_rate:format=duration,bit_rate", "-of", "json", str(video)],
                       capture_output=True, text=True, timeout=60)
    info = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    v = (info.get("streams") or [{}])[0]
    fmt = info.get("format") or {}
    if _bitrate_bps(video, v.get("bit_rate"), fmt.get("bit_rate"), _float(fmt.get("duration"))) <= MAX_BPS:
        return video
    copy = video.with_name("reel.mp4")
    if not copy.exists() or copy.stat().st_mtime_ns < video.stat().st_mtime_ns:
        part = video.with_name("reel.part.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-c:v", "libx264", "-preset", "medium",
                        "-crf", "18", "-maxrate", "20M", "-bufsize", "40M", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", str(part)], check=True, timeout=1800)
        part.replace(copy)
    return copy


def _container(graph: Graph, caption: str) -> str:
    return graph.call("POST", f"{graph.user}/media", media_type="REELS", upload_type="resumable", caption=caption,
                      share_to_feed="true", thumb_offset="0",  # the hook is on screen at 0 s
                      # Meta's AI tag: every channel narrates with an AI voice (5.8; operator, 7 Nov: "Tag on")
                      is_ai_generated="true")["id"]  # ponytail: one value; a per-channel setting if a human ever narrates


def _send(graph: Graph, cid: str, video: Path) -> None:
    """The whole file to rupload in one request; a Reel is well under its 1 GB cap."""
    graph.call("POST", f"{graph.upload}/{cid}", body=video.read_bytes(), timeout=900,
               headers={"Authorization": f"OAuth {graph.token}", "offset": "0", "file_size": str(video.stat().st_size)})


def _status(graph: Graph, cid: str) -> tuple[str, str]:
    r = graph.call("GET", cid, fields="status_code,status")
    return r.get("status_code", ""), r.get("status", "")


def _wait(graph: Graph, cid: str, poll_s: float = 10, wait_s: float = 600) -> tuple[str, str]:
    """Poll until the container is FINISHED, ERROR or EXPIRED; after wait_s it is still processing."""
    deadline = time.monotonic() + wait_s
    while (got := _status(graph, cid))[0] not in ("FINISHED", "ERROR", "EXPIRED"):
        if time.monotonic() > deadline:
            raise InstagramError(f"container {cid} still {got[0] or 'processing'} after {wait_s:.0f} s; the next try "
                                 "polls it again")
        time.sleep(poll_s)
    return got


def _found(graph: Graph, cid: str, caption: str) -> str:
    """The post a PUBLISHED container became: the one among the account's 5 latest media with its caption."""
    want = " ".join(caption.split())
    for m in graph.call("GET", f"{graph.user}/media", fields="id,caption", limit="5").get("data", []):
        if " ".join((m.get("caption") or "").split()) == want:
            return m["id"]
    raise InstagramError(f"container {cid} is PUBLISHED, but none of the 5 latest posts has its caption: check the "
                         "account and record the post by hand; it is never posted again")


def _limit(graph: Graph) -> None:
    """Meta's publishing limit, read before media_publish; at the limit the finished container waits."""
    d = (graph.call("GET", f"{graph.user}/content_publishing_limit", fields="quota_usage,config").get("data") or [{}])[0]
    used, total = d.get("quota_usage", 0), (d.get("config") or {}).get("quota_total", 50)
    if used >= total:
        raise PublishLimit(f"Meta's publishing limit is reached ({used} of {total} posts in 24 h); the Reel waits")


def _record(job: Job, ledger: Path, done: Path, record: dict) -> dict:
    """instagram.json, then this job's one ledger line (written again if a crash fell between the two)."""
    _save(done, record)
    if not any(r.get("job") == job.id for r in _rows(ledger, "instagram")):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return record


def publish_reel(job: Job, video: Path, caption: str, *, graph: Graph, poll_s: float = 10,
                 wait_s: float = 600) -> dict:
    """Post the job's video as a Reel, once. A record returns at once; a session resumes; a container already
    PUBLISHED is found by its caption and never published again. media_publish runs held in jobs.posting: a job dropped
    by then raises jobs.Dropped, posting nothing, and no drop lands while it runs."""
    if reasons := publish.guard(job):  # before fit() and any Graph call
        raise publish.GuardRefused(f"{job.id} will not post: {'; '.join(reasons)}")
    ledger, done, state = job.root / "uploads.jsonl", job.dir / "instagram.json", job.dir / "instagram_session.json"
    prior = json.loads(done.read_text(encoding="utf-8")) if done.exists() else next(
        (r for r in _rows(ledger, "instagram") if r.get("job") == job.id), None)
    if prior is not None:
        return _record(job, ledger, done, prior)
    ws = load(job.workspace)
    if why := off(ws):
        raise ConsentMissing(f"Instagram is {why} for {ws['name']}")
    if not (job.dir / "approval.json").exists():
        raise ConsentMissing("a Reel posts only as the tick's pick (its approval.json record)")
    video = fit(video)  # under Reels' bitrate cap (FAILURE_MODES.md, instagram 18)
    size, sha = video.stat().st_size, hashlib.sha256(video.read_bytes()).hexdigest()
    saved = json.loads(state.read_text(encoding="utf-8")) if state.exists() else None
    if saved:
        code, _ = _status(graph, saved["container_id"])
        if code == "PUBLISHED":  # a cut after media_publish: find the post, never publish it again, changed file or not
            return _record(job, ledger, done, _new(job, graph, _found(graph, saved["container_id"], caption), saved))
        if (saved["size"], saved["sha256"]) != (size, sha):
            raise InstagramError(f"{video.name} changed since its Reel session started ({saved['size']} -> {size} "
                                 f"bytes); delete {state.name} to post the new file")
        saved = None if code == "EXPIRED" else {**saved, "uploaded": saved["uploaded"] or code == "FINISHED"}
    if saved is None:
        if problems := preflight(video):
            raise InstagramError(f"{video.name} won't go as a Reel: {'; '.join(problems)}")
        consent = next(c for c in ws["consent"] if c["action"] == "upload_public_scheduled")
        saved = {"container_id": _container(graph, caption), "size": size, "sha256": sha,
                 "consent_id": consent.get("id"), "uploaded": False, "at": _now()}
        _save(state, saved)  # before the first byte
    cid = saved["container_id"]
    if not saved["uploaded"]:
        try:
            _send(graph, cid, video)
        except TokenError:
            raise
        except InstagramError as e:  # rupload can answer 400 after the bytes landed (7 Nov): the container decides
            _landed(graph, state, cid, e, poll_s, wait_s)
        _save(state, saved := {**saved, "uploaded": True})
    code, text = _wait(graph, cid, poll_s, wait_s)
    if code != "FINISHED":  # an ERROR or EXPIRED container can never post: the next try starts a fresh one
        state.unlink(missing_ok=True)
        raise InstagramError(f"container {cid} is {code}: {text}")
    _limit(graph)
    with jobs.posting(job):  # the one step that can't be undone: a dropped job stops here, and no drop lands inside
        return _record(job, ledger, done, _new(job, graph, graph.call("POST", f"{graph.user}/media_publish",
                                                                      creation_id=cid)["id"], saved))


def _landed(graph: Graph, state: Path, cid: str, error: InstagramError, poll_s: float, wait_s: float) -> None:
    """After a failed send: return once the container is FINISHED (the bytes arrived anyway), else raise the send's
    error. An ERROR or EXPIRED container's session is dropped, so the next try starts a fresh one."""
    try:
        code, text = _wait(graph, cid, poll_s, wait_s)
    except InstagramError:
        raise error from None  # no bytes, or still processing: the next try asks the container again
    if code != "FINISHED":
        state.unlink(missing_ok=True)
        raise InstagramError(f"{error}; container {cid} is {code}: {text}") from None


def _new(job: Job, graph: Graph, media_id: str, saved: dict) -> dict:
    try:
        permalink = graph.call("GET", media_id, fields="permalink").get("permalink")
    except InstagramError:
        permalink = None  # media_publish already succeeded: never skip recording the post on a permalink fetch fault
    record = {"platform": "instagram", "job": job.id, "media_id": media_id,
              "permalink": permalink,
              "container_id": saved["container_id"], "consent_id": saved["consent_id"], "at": _now()}
    if (job.dir / "approval.json").exists():  # the tick's pick record (kind auto, its slot)
        record["approval"] = json.loads((job.dir / "approval.json").read_text(encoding="utf-8"))
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
