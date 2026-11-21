"""Live stream chat-spike recorder and auto-clipper for YouTube and Twitch.

Features:
  - Keyless anonymous Twitch IRC chat tailing (justinfan nick, TLS 6697 with 6667 fallback)
  - YouTube live_chat tailing via yt-dlp
  - Zero-reencode (-c copy) 60s MPEG-TS segment recording (configurable max height)
  - Real-time chat spike detection with raid/giveaway/ad noise filtering
  - Optional automatic queuing of each detected spike clip directly into an autovideo
    `clip` channel with local faster-whisper captions so it appears on Console Home.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import OUT, jobs, locks
from ..workspace import format_of, load
from . import default_spec
from .radar import _ytdlp_base

HYPE_RE = re.compile(r"(?i:LMAO|KEKW|LOL|CLIP|HOLY|NO WAY)|\bW+\b|[💀😂🔥]")
NOISE_RE = re.compile(
    r"(?i)giveaway|\braid(ing|ed)?\b|\bbrb\b|be right back|ad break|sponsor|subscribe|discord\.gg|https?://|promo code|follow me"
)
STOP_WORDS = frozenset(
    "the a an and or to of is it in i you that this on for with was are be so but not my me we he she they what just like at as if do get".split()
)
WORD_RE = re.compile(r"[\w']{2,}|[💀😂🔥]")



LIVE_STATUS_FILE = "clip_live_watchers.json"


def _atomic_live(path: Path, data: object) -> None:
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def update_live_status(
    watch_id: str,
    *,
    channel: str | None = None,
    url: str = "",
    stage: str = "watching",
    detail: str = "",
    spikes: int | None = None,
    pid: int | None = None,
    error: str | None = None,
    root: Path = OUT,
) -> dict[str, Any]:
    """Atomically record live stream watcher status in OUT/clip_live_watchers.json under an OS lock."""
    path = root / LIVE_STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with locks.held(root / "clip_live.lock", wait=True, poll_s=0.1):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prev = data.get(watch_id) if isinstance(data.get(watch_id), dict) else {}
        entry = {
            "id": watch_id,
            "channel": channel if channel is not None else prev.get("channel", ""),
            "url": url or prev.get("url", ""),
            "stage": stage,
            "detail": detail,
            "spikes": int(spikes) if spikes is not None else int(prev.get("spikes") or 0),
            "pid": int(pid) if pid is not None else prev.get("pid"),
            "error": error,
            "started_at": prev.get("started_at") or now,
            "updated_at": now,
        }
        data[watch_id] = entry
        if len(data) > 20:
            keep = sorted(data.values(), key=lambda x: str(x.get("updated_at") or ""), reverse=True)[:20]
            data = {x["id"]: x for x in keep if x.get("id")}
        _atomic_live(path, data)
        return entry


def stop_flag_path(watch_id: str, *, root: Path = OUT) -> Path:
    return root / "clip_live" / f"{watch_id}.stop"

def twitch_login(url: str) -> str | None:
    """Extract lowercase Twitch channel login from a live channel URL (not /videos/)."""
    m = re.match(r"https?://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)/?(?:[?#].*)?$", url.strip())
    if not m:
        return None
    login = m.group(1).lower()
    return None if login in ("videos", "directory") else login


def _parse_privmsg(line: str) -> tuple[str, str] | None:
    """Parse Twitch IRC PRIVMSG line into (user, text) or None."""
    tags = ""
    if line.startswith("@"):
        tags, _, line = line.partition(" ")
    if " PRIVMSG " not in line:
        return None
    prefix, _, rest = line.partition(" PRIVMSG ")
    user = prefix.lstrip(":").split("!")[0]
    for t in tags.lstrip("@").split(";"):
        if t.startswith("display-name=") and t[13:]:
            user = t[13:]
    _, _, text = rest.partition(" :")
    return user, text.strip()


def irc_chat(
    channel: str,
    out_path: Path,
    stop_event: threading.Event | None = None,
    *,
    host: str = "irc.chat.twitch.tv",
    port: int = 6697,
) -> None:
    """Read-only anonymous Twitch IRC chat listener appending {t, user, text} JSONL."""
    stop_event = stop_event or threading.Event()
    chan = channel.lower().lstrip("#")
    use_tls = True
    with open(out_path, "a", encoding="utf-8", buffering=1) as out:
        while not stop_event.is_set():
            sock = None
            try:
                raw = socket.create_connection((host, port if use_tls else 6667), timeout=15)
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if use_tls else raw
                sock.settimeout(5)
                sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                sock.sendall(f"JOIN #{chan}\r\n".encode())
                buf = b""
                while not stop_event.is_set():
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for ln in lines:
                        s = ln.decode("utf-8", "replace")
                        if s.startswith("PING"):
                            sock.sendall(("PONG" + s[4:] + "\r\n").encode())
                            continue
                        m = _parse_privmsg(s)
                        if m and m[1]:
                            out.write(
                                json.dumps({"t": time.time(), "user": m[0], "text": m[1]}, ensure_ascii=False) + "\n"
                            )
            except (OSError, ssl.SSLError) as ex:
                if use_tls and not isinstance(ex, socket.timeout):
                    use_tls = False
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            stop_event.wait(3)


def _extract_yt_text(renderer: dict[str, Any]) -> str:
    parts = []
    for r in (renderer.get("message") or {}).get("runs", []):
        if "text" in r:
            parts.append(str(r["text"]))
        elif "emoji" in r:
            e = r["emoji"]
            eid = str(e.get("emojiId") or "")
            parts.append(eid if len(eid) <= 2 else str((e.get("shortcuts") or [""])[0]))
    return "".join(parts).strip()


class ChatTail:
    """Tails yt-dlp's live_chat JSONL output and aggregates per-second message rates."""

    def __init__(self, work_dir: Path, start_wall: float) -> None:
        self.d = work_dir
        self.start = start_wall
        self.pos: dict[str, int] = {}
        self.buf: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self.raw: Counter[int] = Counter()
        self.wgt: Counter[int] = Counter()
        self.words: dict[int, list[str]] = {}

    def poll(self) -> None:
        for f in sorted(self.d.glob("chat.live_chat.json*")):
            try:
                with open(f, "rb") as fh:
                    fh.seek(self.pos.get(f.name, 0))
                    data = fh.read()
            except OSError:
                continue
            if not data:
                continue
            self.pos[f.name] = self.pos.get(f.name, 0) + len(data)
            buf = self.buf.get(f.name, b"") + data
            *lines, rest = buf.split(b"\n")
            self.buf[f.name] = rest
            for ln in lines:
                self._handle_yt(ln)

    def _record(self, ts: float, txt: str) -> None:
        if ts < self.start - 5 or not txt or NOISE_RE.search(txt):
            return
        sec = int(ts)
        self.raw[sec] += 1
        self.wgt[sec] += 2 if HYPE_RE.search(txt) else 1
        self.words.setdefault(sec, []).append(txt)

    def _handle_yt(self, ln: bytes) -> None:
        try:
            acts = json.loads(ln)["replayChatItemAction"]["actions"]
        except Exception:
            return
        for a in acts:
            r = (((a.get("addChatItemAction") or {}).get("item")) or {}).get("liveChatTextMessageRenderer")
            if not r:
                continue
            mid = r.get("id")
            if mid and mid in self.seen:
                continue
            if mid:
                self.seen.add(mid)
            try:
                ts = int(r["timestampUsec"]) / 1e6
            except Exception:
                ts = time.time()
            self._record(ts, _extract_yt_text(r))

    def total(self, counter: Counter[int], a: int, b: int) -> int:
        return sum(counter.get(s, 0) for s in range(a, b + 1))

    def top_words(self, a: int, b: int, n: int = 3) -> list[str]:
        cnt: Counter[str] = Counter()
        for s in range(a, b + 1):
            for t in self.words.get(s, []):
                cnt.update(w.lower() for w in WORD_RE.findall(t) if w.lower() not in STOP_WORDS)
        return [w for w, _ in cnt.most_common(n)]


class TwitchTail(ChatTail):
    """Tails JSONL appended by irc_chat() into chat.twitch.jsonl."""

    def poll(self) -> None:
        f = self.d / "chat.twitch.jsonl"
        try:
            with open(f, "rb") as fh:
                fh.seek(self.pos.get(f.name, 0))
                data = fh.read()
        except OSError:
            return
        if not data:
            return
        self.pos[f.name] = self.pos.get(f.name, 0) + len(data)
        buf = self.buf.get(f.name, b"") + data
        *lines, rest = buf.split(b"\n")
        self.buf[f.name] = rest
        for ln in lines:
            try:
                m = json.loads(ln)
                ts, txt = float(m["t"]), str(m.get("text") or "").strip()
            except Exception:
                continue
            self._record(ts, txt)


class StreamRecorder:
    """Records a live stream into rolling 60s MPEG-TS segments with zero re-encoding (-c copy)."""

    def __init__(self, live_dir: Path, url: str, minutes: float, *, max_height: int = 1080) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH; required for live stream recording")
        self.ld = live_dir
        self.csv = live_dir / "segments.csv"
        fmt = f"b[height<={int(max_height)}]/bv*[height<={int(max_height)}]+ba/b"
        r = subprocess.run(
            [*_ytdlp_base(), "-g", "-f", fmt, url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        urls = [u for u in r.stdout.split() if u.startswith("http")]
        if r.returncode != 0 or not urls:
            raise RuntimeError(f"Could not resolve live stream URL (is the stream live?): {(r.stderr or '')[-300:]}")
        cmd = [ffmpeg, "-y", "-loglevel", "warning"]
        for u in urls:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-i", u]
        for i in range(len(urls)):
            cmd += ["-map", str(i)]
        cmd += [
            "-c", "copy",
            "-t", str(int(minutes * 60)),
            "-f", "segment",
            "-segment_time", "60",
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            "-segment_list", str(self.csv),
            "-segment_list_type", "csv",
            str(live_dir / "seg_%05d.ts"),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.t0: float | None = None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b"q\n")
                    self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def segments(self) -> list[tuple[Path, float, float, float, bool]]:
        """Return [(path, start_s, end_s, mtime, complete)] along cumulative recording timeline."""
        done: list[tuple[Path, float, float, float, bool]] = []
        t = 0.0
        if self.csv.exists():
            for ln in self.csv.read_text(encoding="utf-8", errors="ignore").splitlines():
                p = ln.split(",")
                if len(p) >= 3:
                    try:
                        dur = max(0.0, float(p[2]) - float(p[1]))
                    except ValueError:
                        continue
                    f = self.ld / p[0]
                    if f.exists():
                        done.append((f, t, t + dur, f.stat().st_mtime, True))
                    t += dur
        names = {s[0].name for s in done}
        for f in sorted(self.ld.glob("seg_*.ts")):
            if f.name not in names:
                done.append((f, t, float("inf"), f.stat().st_mtime, False))
        return done


def _prune_old_segments(rec: StreamRecorder, *, keep_s: float = 240.0, has_pending: bool = False) -> int:
    """Delete completed seg_*.ts files older than keep_s (rolling ring buffer so disk stays ~150-200 MB)."""
    if has_pending:
        return 0
    comp = [seg for seg in rec.segments() if seg[4]]
    if len(comp) <= 4:
        return 0
    latest_end = comp[-1][2]
    cutoff = latest_end - max(120.0, keep_s)
    removed = 0
    for path, _st, en, _mt, _done in comp[:-4]:
        if en < cutoff:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def _cut_spike(
    rec: StreamRecorder,
    wall: float,
    before: float,
    after: float,
    lag: float,
    out_mp4: Path,
) -> tuple[float | None, bool]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, False
    segs = rec.segments()
    comp = [s for s in segs if s[4]]
    if not comp:
        return None, False
    _, _, en, mt, _ = comp[-1]
    center = en - (mt - wall) + lag
    t0, t1 = max(0.0, center - before), center + after
    pick = [s for s in segs if s[2] > t0 and s[1] < t1]
    if not pick:
        return center, False
    lst = out_mp4.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{s[0].resolve().as_posix()}'\n" for s in pick), encoding="utf-8")
    try:
        r = subprocess.run(
            [
                ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-ss", f"{max(0.0, t0 - pick[0][1]):.2f}", "-t", f"{t1 - t0:.2f}",
                "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out_mp4),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        lst.unlink(missing_ok=True)
    return center, (r.returncode == 0 and out_mp4.is_file() and out_mp4.stat().st_size > 1000)


def _probe_video_dims(mp4: Path) -> tuple[int, int, float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 1920, 1080, 0.0
    r = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(mp4),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return 1920, 1080, 0.0
    try:
        info = json.loads(r.stdout)
        dur = float((info.get("format") or {}).get("duration") or 0.0)
        vs = next((s for s in (info.get("streams") or []) if s.get("codec_type") == "video"), {})
        return int(vs.get("width") or 1920), int(vs.get("height") or 1080), dur
    except Exception:
        return 1920, 1080, 0.0


def _score_live_spike(
    transcript: dict[str, Any],
    *,
    dur: float,
    creator: str,
    rate: int,
    words: list[str],
    niche: str = "",
    cwd: Path | None = None,
    llm_fn=None,
) -> dict[str, Any]:
    """Run a fast single-call LLM virality gate on a ~45s live spike transcript.

    Returns {keep, score, start_s, end_s, title, hook, reason}.
    """
    segs = transcript.get("segments") or []
    lines = [
        f"[{float(seg.get('start', 0)):.1f}s-{float(seg.get('end', 0)):.1f}s] {str(seg.get('text') or '').strip()}"
        for seg in segs
        if str(seg.get("text") or "").strip()
    ]
    tx = "\n".join(lines) if lines else "(no speech detected)"
    tag = ", ".join(words[:5]) if words else "chat spike"
    editor_role = f"viral {niche.strip()} Shorts editor" if niche.strip() else "viral short-form stream clip editor"
    prompt = (
        f"You are a {editor_role} evaluating a {dur:.1f}s live stream clip from {creator or 'a streamer'}.\n"
        f"Live chat spiked ({rate} msgs/10s; top chat words: {tag}).\n\n"
        f"Transcript:\n{tx}\n\n"
        "Decide if this is a genuinely viral, funny, chaotic, or high-energy Short (score 0-100).\n"
        "Reject (keep=false, score<75) if it is mostly sub alerts, reading chat, menu navigation, mid banter with no payoff, "
        "or explicit NSFW/nudity content.\n"
        "If keeping it, pick the tightest start_s and end_s (12-45s) starting right on the action/reaction and ending right "
        "after the punchline, and write a 3-6 word viral hook with 1 punchy word wrapped in *asterisks* "
        "(e.g. 'This play was *impossible* to survive').\n"
        "Reply ONLY with valid JSON: "
        '{"keep": true, "score": 85, "start_s": 2.0, "end_s": 34.0, "title": "SEO title", "hook": "3-6 word *hook*", "reason": "short reason"}'
    )
    if llm_fn is None:
        from .. import ROOT
        from ..llm.router import route_call
        raw = route_call("clip:topics", prompt, cwd=cwd or ROOT).text
    else:
        raw = llm_fn(prompt)
    m = re.search(r"\{[\s\S]*\}", str(raw or ""))
    if not m:
        raise ValueError("LLM returned no JSON object")
    data = json.loads(m.group(0))
    return {
        "keep": bool(data.get("keep", False)),
        "score": float(data.get("score") or 0.0),
        "start_s": float(data.get("start_s") or 0.0),
        "end_s": float(data.get("end_s") or dur),
        "title": str(data.get("title") or "").strip(),
        "hook": str(data.get("hook") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
    }


def _queue_spike_job(
    channel: str,
    url: str,
    spike_mp4: Path,
    *,
    num: int,
    rate: int,
    words: list[str],
    root: Path = OUT,
    llm_fn=None,
) -> str:
    """Transcribe a captured live spike clip locally, run an LLM virality gate (>=75), and queue winners."""
    from .pipeline import _atomic, _transcribe_local

    ws = load(channel)
    if format_of(ws)["pipeline"] != "clip":
        raise ValueError(f"{channel} uses pipeline {format_of(ws)['pipeline']!r}, not 'clip'")
    cfg = ws.get("clip") or {}
    min_score = float(cfg.get("live_min_score", 75.0))
    w, h, dur = _probe_video_dims(spike_mp4)
    transcript = _transcribe_local(
        spike_mp4, model_size=cfg.get("whisper_model", "base"), language=ws.get("language", "en")
    )
    total_dur = float(transcript.get("duration") or dur or 45.0)
    login = twitch_login(url) or ""
    creator = login or "streamer"
    tag = ", ".join(words[:3]) if words else "chat spike"

    verdict = _score_live_spike(
        transcript, dur=total_dur, creator=creator, rate=rate, words=words,
        niche=str(cfg.get("live_niche") or ""), cwd=spike_mp4.parent, llm_fn=llm_fn,
    )
    if not verdict["keep"] or verdict["score"] < min_score:
        spike_mp4.unlink(missing_ok=True)
        return f"skipped:{int(round(verdict['score']))}"

    start_s = max(0.0, min(verdict["start_s"], max(0.0, total_dur - 10.0)))
    end_s = max(start_s + 10.0, min(verdict["end_s"], total_dur))
    if end_s - start_s < 10.0:
        start_s, end_s = 0.0, total_dur

    title = verdict["title"] or f"{creator} Live Moment #{num}: {tag}"
    hook = verdict["hook"] or title
    reason = verdict["reason"] or f"Live chat spike ({rate} msgs; top words: {tag})"
    score = max(0.0, min(100.0, verdict["score"]))

    brief_md = spike_mp4.with_suffix(".md")
    brief_md.write_text(
        f"# Live Clip: {title}\n\nSource: {url}\nCreator: {creator}\n"
        f"Chat rate: {rate} msgs\nTop words: {tag}\nReason: {reason}\n",
        encoding="utf-8",
    )
    jid = jobs.new(channel, brief_md, root=root)
    d = root / "jobs" / jid
    spec = default_spec(
        source_url=url,
        source_path=str(spike_mp4.resolve()),
        source_width=w,
        source_height=h,
        start_s=round(start_s, 2),
        end_s=round(end_s, 2),
        title=title,
        hook=hook,
        reason=reason,
        score=round(score, 1),
        layout=cfg.get("default_layout", "auto"),
        caption_style=cfg.get("caption_style", "viral"),
        pacing=cfg.get("pacing", "natural"),
        speed=float(cfg.get("speed", 1.0)),
        include_captions=bool(cfg.get("include_captions", True)),
        include_title=bool(cfg.get("include_title", True)),
        captions=transcript.get("segments", []),
        normalize_audio=bool(cfg.get("normalize_audio", True)),
        allow_profanity=bool(cfg.get("allow_profanity", False)),
        title_stop_words=cfg.get("title_stop_words"),
    )
    if login:
        spec["creator"] = login
        spec["creator_handle"] = f"@{login}"
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic(d / "clip_spec.json", spec)
    _atomic(d / "topic.json", {
        "key": title,
        "score": spec["score"],
        "lever": "live_chat_spike",
        "source_url": url,
    })
    _atomic(d / "review.json", {"block": False, "defects": [], "source": "live_chat_spike", "at": at})
    return jid


def watch_live(
    url: str,
    *,
    channel: str | None = None,
    minutes: float = 60.0,
    max_height: int = 1080,
    before: float = 20.0,
    after: float = 25.0,
    window: int = 10,
    baseline: int = 300,
    ratio: float = 2.3,
    min_msgs: int = 8,
    cooldown: float = 45.0,
    lag: float = 8.0,
    auto_queue: bool = True,
    watch_id: str | None = None,
    root: Path = OUT,
) -> dict[str, Any]:
    """Watch a live YouTube or Twitch stream, cutting clips on chat spikes and optionally queuing jobs."""
    if channel and auto_queue:
        ws = load(channel)
        if format_of(ws)["pipeline"] != "clip":
            raise ValueError(f"{channel} uses pipeline {format_of(ws)['pipeline']!r}, not 'clip'")

    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    ld = root / "clip_live" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{key}"
    ld.mkdir(parents=True, exist_ok=True)
    spikes_md = ld / "spikes.md"
    spikes_md.write_text(
        f"| UTC time | offset | msgs/{window}s | top words | file | job |\n|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )

    def _stat(stage: str, detail: str = "", *, spikes_n: int | None = None, error: str | None = None) -> None:
        if watch_id:
            update_live_status(
                watch_id, channel=channel, url=url, stage=stage, detail=detail,
                spikes=spikes_n, pid=os.getpid(), error=error, root=root,
            )

    stop_file = stop_flag_path(watch_id, root=root) if watch_id else None
    if stop_file:
        stop_file.unlink(missing_ok=True)
    _stat("watching", f"Connecting to {url}", spikes_n=0)

    start = time.time()
    end_at = start + minutes * 60.0 + 30.0
    try:
        rec = StreamRecorder(ld, url, minutes, max_height=max_height)
    except Exception as ex:
        _stat("error", str(ex)[:240], error=f"{type(ex).__name__}: {str(ex)[:240]}")
        raise
    _stat("watching", f"Recording stream & tailing live chat ({minutes:g}m)", spikes_n=0)

    login = twitch_login(url)
    stop_chat = threading.Event()
    chat_thread: threading.Thread | None = None
    chat_proc: subprocess.Popen | None = None
    if login:
        chat_thread = threading.Thread(target=irc_chat, args=(login, ld / "chat.twitch.jsonl", stop_chat), daemon=True)
        chat_thread.start()
        tail: ChatTail = TwitchTail(ld, start)
    else:
        chat_proc = subprocess.Popen(
            [*_ytdlp_base(), "--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(ld / "chat"), url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        tail = ChatTail(ld, start)

    pending: list[tuple[int, float, int, list[str], float]] = []
    captured: list[dict[str, Any]] = []
    last_spike = 0.0
    n = 0

    print(f"Watching {url} for {minutes:g}m -> {ld} (Ctrl+C to stop)", flush=True)

    def _finish(item: tuple[int, float, int, list[str]]) -> None:
        num, wall, rate, words = item
        out_mp4 = ld / f"spike_{num:03d}.mp4"
        center, ok = _cut_spike(rec, wall, before, after, lag, out_mp4)
        iso = datetime.fromtimestamp(wall, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        off = "-" if center is None else f"{int(center // 60)}:{int(center % 60):02d}"
        jid = ""
        if ok and channel and auto_queue:
            try:
                jid = _queue_spike_job(channel, url, out_mp4, num=num, rate=rate, words=words, root=root)
            except Exception as ex:
                jid = f"queue_err:{type(ex).__name__}"
        with open(spikes_md, "a", encoding="utf-8") as fh:
            fh.write(f"| {iso} | {off} | {rate} | {', '.join(words)} | {out_mp4.name if ok else 'CUT FAILED'} | {jid} |\n")
        captured.append({"num": num, "offset": off, "rate": rate, "words": words, "file": str(out_mp4) if ok else "", "job": jid})
        _stat(
            "watching",
            f"Captured {len(captured)} spike(s) — last: {', '.join(words[:3]) or off}",
            spikes_n=len(captured),
        )
        print(
            f"{'CLIP ' + out_mp4.name if ok else 'CUT FAILED'} (offset {off}, {rate} msgs/{window}s, {words})"
            + (f" -> queued job {jid}" if jid else ""),
            flush=True,
        )

    stopped_by_user = False
    last_prune = time.time()
    last_beat = time.time()
    try:
        while time.time() < end_at and rec.alive():
            if stop_file and stop_file.is_file():
                stopped_by_user = True
                _stat("stopping", "Stopping live watcher & flushing pending spikes", spikes_n=len(captured))
                break
            time.sleep(1.0)
            tail.poll()
            t = time.time()
            if rec.t0 is None and any(ld.glob("seg_*.ts")):
                rec.t0 = t
            e = int(t) - 3
            elapsed_chat = e - int(start)
            if elapsed_chat >= window + 30 and (t - last_spike) >= cooldown and rec.t0 is not None:
                raw = tail.total(tail.raw, e - window + 1, e)
                w = tail.total(tail.wgt, e - window + 1, e)
                nb = max(1, min(baseline, elapsed_chat - window))
                bw = tail.total(tail.wgt, e - window - nb + 1, e - window) / nb
                rate_w = w / window
                if raw >= min_msgs and rate_w >= ratio * max(bw, 0.02):
                    n += 1
                    last_spike = t
                    words = tail.top_words(e - window + 1, e)
                    print(f"SPIKE #{n}: {raw} msgs/{window}s vs baseline {bw * window:.1f} weighted ({words})", flush=True)
                    pending.append((n, float(e), raw, words, t + after + max(lag, 0.0)))
            ready = [p for p in pending if p[4] <= t]
            for p in ready:
                pending.remove(p)
                _finish(p[:4])
            if t - last_prune >= 30.0:
                last_prune = t
                _prune_old_segments(rec, keep_s=240.0, has_pending=bool(pending))
            if t - last_beat >= 60.0:
                last_beat = t
                _stat(
                    "watching",
                    f"Captured {len(captured)} spike(s) — watching ({minutes:g}m)" if captured else f"Recording stream & tailing live chat ({minutes:g}m)",
                    spikes_n=len(captured),
                )
    except KeyboardInterrupt:
        print("Stopping live watcher...", flush=True)
    finally:
        rec.stop()
        if chat_thread is not None:
            stop_chat.set()
            chat_thread.join(5)
        elif chat_proc is not None and chat_proc.poll() is None:
            chat_proc.terminate()
            try:
                chat_proc.wait(10)
            except subprocess.TimeoutExpired:
                chat_proc.kill()
        for p in pending:
            _finish(p[:4])
        for seg_file in ld.glob("seg_*.ts"):
            try:
                seg_file.unlink(missing_ok=True)
            except OSError:
                pass

    if stop_file:
        stop_file.unlink(missing_ok=True)
    _stat(
        "stopped" if stopped_by_user else "done",
        f"Finished with {len(captured)} spike clip(s)",
        spikes_n=len(captured),
    )
    summary = {"url": url, "channel": channel, "dir": str(ld), "spikes": len(captured), "clips": captured}
    (ld / "live.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
