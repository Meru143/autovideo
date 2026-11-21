"""VOD and live-stream clipping pipeline wiring download, local Whisper transcription,
LLM highlight scoring, smart layout analysis, captioning, and rendering into autovideo jobs."""
from __future__ import annotations

import asyncio
import array
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import OUT, jobs, locks
from ..claude_call import SENSOR, call
from ..render import contact_sheet
from ..workspace import format_of, load
from . import default_spec


def _atomic(path: Path, data: object) -> None:
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


LONG_VOD_THRESHOLD_S = 7200.0  # > 2h VODs auto-use audio-first + targeted 1080p section downloads
INGEST_STATUS_FILE = "clip_ingests.json"


STAGE_PROGRESS = {
    "starting": 5,
    "probing": 12,
    "downloading": 35,
    "transcribing": 60,
    "scoring": 78,
    "slicing": 88,
    "done": 100,
}


def update_ingest_status(
    ingest_id: str,
    *,
    channel: str,
    url: str,
    stage: str,
    detail: str = "",
    title: str | None = None,
    queued: int = 0,
    progress: int | None = None,
    error: str | None = None,
    root: Path = OUT,
) -> dict:
    """Atomically record live VOD ingest progress in OUT/clip_ingests.json under an OS lock."""
    path = root / INGEST_STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with locks.held(root / "clip_ingests.lock", wait=True, poll_s=0.1):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prev = data.get(ingest_id) if isinstance(data.get(ingest_id), dict) else {}
        pct = progress if progress is not None else STAGE_PROGRESS.get(stage, int(prev.get("progress") or 0))
        entry = {
            "id": ingest_id,
            "channel": channel,
            "url": url,
            "title": (title if title is not None else prev.get("title") or ""),
            "stage": stage,
            "detail": detail,
            "progress": max(0, min(100, int(pct))),
            "queued": int(queued),
            "error": error,
            "started_at": prev.get("started_at") or now,
            "updated_at": now,
        }
        data[ingest_id] = entry
        if len(data) > 20:
            keep = sorted(data.values(), key=lambda x: str(x.get("updated_at") or ""), reverse=True)[:20]
            data = {x["id"]: x for x in keep if x.get("id")}
        _atomic(path, data)
        return entry
SLICE_PAD_S = 10.0             # +/- 10s padding around each clip slice for Clip Editor scrubbing



SEEN_CLIPS_FILE = "clip_seen.json"


def _extract_creator(info: dict) -> tuple[str, str]:
    """Extract (display_name, @handle) from yt-dlp metadata for Twitch/YouTube streams."""
    name = str(info.get("channel") or info.get("uploader") or info.get("creator") or "").strip()
    raw_handle = str(info.get("uploader_id") or info.get("channel_id") or "").strip()
    if raw_handle.startswith("UC") and len(raw_handle) >= 20:
        raw_handle = ""  # YouTube opaque channel ID, not a readable handle
    handle = ("@" + raw_handle.lstrip("@")) if raw_handle else (("@" + re.sub(r"\s+", "", name)) if name else "")
    return name, handle


def _canonical_source_key(url: str) -> str:
    """Normalize YouTube/Twitch VOD URLs so tracking params don't bypass deduplication."""
    u = url.strip()
    if m := re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?[^#]*v=|live/|shorts/))([A-Za-z0-9_-]{6,})", u):
        return f"yt:{m.group(1)}"
    if m := re.search(r"twitch\.tv/videos/(\d+)", u):
        return f"twitch_vod:{m.group(1)}"
    if m := re.match(r"https?://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)/?(?:[?#].*)?$", u):
        return f"twitch_live:{m.group(1).lower()}"
    return u.lower()


def _overlaps_existing(start_s: float, end_s: float, existing: list[tuple[float, float]], *, threshold: float = 0.4) -> bool:
    dur = max(1.0, float(end_s) - float(start_s))
    for ex_s, ex_e in existing:
        ov = min(float(end_s), ex_e) - max(float(start_s), ex_s)
        if ov > 0 and (ov / dur) >= threshold:
            return True
    return False


def _try_reserve_window(
    channel: str,
    source_key: str,
    start_s: float,
    end_s: float,
    tag: str,
    *,
    root: Path = OUT,
    threshold: float = 0.4,
) -> bool:
    """Atomically check overlap and reserve [start_s, end_s] under an OS lock to prevent concurrent duplicate clips."""
    path = root / SEEN_CLIPS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with locks.held(root / "clip_seen.lock", wait=True, poll_s=0.1):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        ch_map = data.setdefault(channel, {})
        if not isinstance(ch_map, dict):
            ch_map = {}
            data[channel] = ch_map
        items = ch_map.setdefault(source_key, [])
        if not isinstance(items, list):
            items = []
            ch_map[source_key] = items
        existing = [
            (float(e["start_s"]), float(e["end_s"]))
            for e in items
            if isinstance(e, dict) and "start_s" in e and "end_s" in e
        ]
        if _overlaps_existing(start_s, end_s, existing, threshold=threshold):
            return False
        items.append({"start_s": round(float(start_s), 2), "end_s": round(float(end_s), 2), "job": tag})
        if len(items) > 200:
            ch_map[source_key] = items[-200:]
        _atomic(path, data)
        return True


def _probe_remote_info(url: str) -> dict:
    """Fast metadata probe via yt-dlp (zero video bytes): duration, dimensions, title, and YouTube heatmap."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        return {}
    r = subprocess.run(
        [yt_dlp, "--no-playlist", "--dump-single-json", "--skip-download", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except ValueError:
        return {}




_SLURS_RE = re.compile(r"\b(nigg\w*|n[i1]gg?a\w*|f[a@]gg?\w*|retard\w*|tranny\w*|chink\w*|spic\w*|kike\w*)\b", re.I)
_SWEARS_RE = re.compile(
    r"\b(fuck\w*|motherfuck\w*|shit\w*|bitch\w*|cunt\w*|dick\w*|pussy\w*|asshole\w*|bastard\w*|whore\w*|slut\w*)\b",
    re.I,
)


def _mask_word(m: re.Match[str]) -> str:
    w = m.group(0)
    return w[0] + "*" * (len(w) - 1)


def clean_caption_text(text: str, *, allow_profanity: bool = False) -> str:
    """Mask slurs unconditionally and hard swears unless allow_profanity=True."""
    out = _SLURS_RE.sub(_mask_word, text)
    return out if allow_profanity else _SWEARS_RE.sub(_mask_word, out)


def _audio_hype_spikes(media_path: Path, *, top_k: int = 15, min_gap_s: float = 30.0) -> list[dict]:
    """Stream 1-second RMS loudness in O(1) RAM via ffmpeg and return top local audio spike timestamps."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not media_path.is_file():
        return []
    proc = subprocess.Popen(
        [ffmpeg, "-v", "error", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    dbs: list[float] = []
    chunk_bytes = 16000 * 2  # 1 second of 16 kHz mono int16 PCM
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(chunk_bytes)
            if len(buf) < chunk_bytes:
                break
            samples = array.array("h")
            samples.frombytes(buf)
            mean_sq = sum(x * x for x in samples) / (len(samples) * (32768.0 * 32768.0))
            dbs.append(10.0 * math.log10(mean_sq + 1e-9))
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()

    n = len(dbs)
    if n < 10:
        return []
    # Rolling 30-second local baseline so sudden screams/laughter pop above steady gameplay audio
    win = min(30, max(5, n // 4))
    half = win // 2
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(dbs):
        prefix[i + 1] = prefix[i] + v
    diffs = []
    for i, v in enumerate(dbs):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        local_avg = (prefix[hi] - prefix[lo]) / (hi - lo)
        diffs.append(v - local_avg)
    sorted_d = sorted(diffs)
    med = sorted_d[n // 2]
    var = sum((x - med) ** 2 for x in diffs) / n
    std = math.sqrt(var)
    if std < 1e-6:
        return []
    zscores = [max(0.0, (x - med) / std) for x in diffs]

    peaks: list[dict] = []
    for sec in sorted(range(n), key=lambda i: zscores[i], reverse=True):
        z = zscores[sec]
        if z < 1.5:
            break
        if any(abs(sec - p["t_s"]) < min_gap_s for p in peaks):
            continue
        peaks.append({"t_s": float(sec), "z_score": round(z, 2), "db_jump": round(diffs[sec], 1)})
        if len(peaks) >= top_k:
            break
    return sorted(peaks, key=lambda x: x["t_s"])


def _has_audio_stream(mp4: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not mp4.is_file():
        return False
    r = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _normalize_loudness(mp4: Path, *, target_i: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> bool:
    """Fast -14 LUFS loudness normalization pass (-c:v copy, zero video re-encode)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not _has_audio_stream(mp4):
        return False
    tmp = mp4.with_name(f"{mp4.stem}.{os.getpid()}.norm{mp4.suffix}")
    try:
        r = subprocess.run(
            [
                ffmpeg, "-y", "-v", "error", "-i", str(mp4),
                "-c:v", "copy",
                "-af", f"loudnorm=I={target_i}:TP={tp}:LRA={lra}",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-movflags", "+faststart", str(tmp),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 1000:
            os.replace(tmp, mp4)
            return True
    finally:
        tmp.unlink(missing_ok=True)
    return False


def _vod_chat_spikes(url: str, cache_dir: Path, *, duration_s: float = 0.0) -> list[dict]:
    """Fetch & cache Twitch VOD chat explosion peaks (fail-soft, returns [] for non-Twitch or on error)."""
    m = re.search(r"twitch\.tv/videos/(\d+)", url)
    if not m:
        return []
    cache = cache_dir / "chat_spikes.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
    try:
        from .radar import twitch_vod_chat_spikes
        spikes = twitch_vod_chat_spikes(m.group(1), duration_s=duration_s)
    except Exception:
        spikes = []
    if spikes:
        _atomic(cache, spikes)
    return spikes


def _heatmap_peaks(info: dict, *, top_k: int = 15, min_gap_s: float = 45.0) -> list[dict]:
    """Extract top viewer-replayed moments from YouTube's heatmap metadata (catches silent visual peaks)."""
    hm = info.get("heatmap")
    if not isinstance(hm, list) or not hm:
        return []
    peaks: list[dict] = []
    for entry in sorted(hm, key=lambda x: float(x.get("value") or 0.0), reverse=True):
        val = float(entry.get("value") or 0.0)
        if val < 0.6:
            break
        start = float(entry.get("start_time") or 0.0)
        end = float(entry.get("end_time") or start)
        if any(abs(start - p["start_s"]) < min_gap_s for p in peaks):
            continue
        peaks.append({"start_s": round(start, 1), "end_s": round(end, 1), "intensity": round(val, 2)})
        if len(peaks) >= top_k:
            break
    return sorted(peaks, key=lambda x: x["start_s"])


def _download_audio_only(
    url: str,
    info: dict,
    *,
    root: Path = OUT,
    live_from_start: bool = False,
    end_s: float | None = None,
    force_refresh: bool = False,
) -> tuple[Path, dict]:
    """Download audio-only (~400-600 MB for a 24h stream instead of 50 GB) for fast local faster-whisper."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise RuntimeError("yt-dlp is not on PATH; required for long-VOD audio-first ingestion")
    cache_tag = f"{url}#live_{int(end_s or 0) // 300}" if force_refresh else url
    key = hashlib.sha256(cache_tag.encode("utf-8")).hexdigest()[:16]
    cache = root / "clip_sources" / key
    cache.mkdir(parents=True, exist_ok=True)
    meta_file = cache / "audio_source.json"
    if not force_refresh and meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        cached = Path(meta.get("audio_path", ""))
        if cached.is_file() and cached.stat().st_size > 0:
            return cached, meta

    out_tmpl = str(cache / "audio.%(ext)s")
    if live_from_start:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH; required for live YouTube DVR catch-up")
        out_m4a = cache / "audio.m4a"
        out_m4a.unlink(missing_ok=True)
        cap_s = max(60.0, float(end_s or 3600.0))
        yt_err_log = cache / "dvr_ytdlp.stderr.log"
        # Write yt-dlp stderr to a file (never an un-drained PIPE, which deadlocks Windows once buffer fills)
        # Download DASH live-from-start fragments directly to disk (piping -o - stalls yt-dlp dashsegments)
        raw_base = cache / "dvr_raw.m4a"
        for leftover in cache.glob("dvr_raw*"):
            leftover.unlink(missing_ok=True)
        # ~16 KB/s for 128 kbps AAC; add headroom so ffmpeg -t cap_s trims cleanly
        max_bytes = int(cap_s * 20_000)
        with yt_err_log.open("wb") as err_f:
            yt_p = subprocess.Popen(
                [
                    yt_dlp, "--no-playlist", "--no-progress", "--live-from-start",
                    "--concurrent-fragments", "4", "-f", "bestaudio/best",
                    "-o", str(raw_base), url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=err_f,
            )
            started = time.monotonic()
            last_size, last_growth = 0, started
            try:
                while yt_p.poll() is None:
                    time.sleep(2.0)
                    now_m = time.monotonic()
                    cur_size = sum(p.stat().st_size for p in cache.glob("dvr_raw*") if p.is_file())
                    if cur_size > last_size + 1024:
                        last_size, last_growth = cur_size, now_m
                    elif now_m - last_growth > 90.0:
                        raise RuntimeError(
                            f"live DVR audio stalled at {cur_size} bytes for >90s (yt-dlp dashsegments hung)"
                        )
                    if cur_size >= max_bytes:
                        break
                    if now_m - started > 3600.0:
                        break
            finally:
                if yt_p.poll() is None:
                    yt_p.terminate()
                    try:
                        yt_p.wait(5)
                    except subprocess.TimeoutExpired:
                        yt_p.kill()
        raw_src = next(
            (p for p in sorted(cache.glob("dvr_raw*"), key=lambda x: x.stat().st_size if x.is_file() else 0, reverse=True)
             if p.is_file() and p.stat().st_size > 1000),
            None,
        )
        if raw_src is None:
            yt_tail = yt_err_log.read_text(encoding="utf-8", errors="replace")[-300:] if yt_err_log.is_file() else ""
            raise RuntimeError(f"live DVR audio download produced no data | yt-dlp: {yt_tail}")
        ff_cmd = [ffmpeg, "-y", "-v", "error", "-i", str(raw_src), "-t", f"{cap_s:.0f}", "-vn", "-c:a", "aac", "-b:a", "128k", str(out_m4a)]
        ff_r = subprocess.run(ff_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        for leftover in cache.glob("dvr_raw*"):
            leftover.unlink(missing_ok=True)
        if ff_r.returncode != 0 or not out_m4a.is_file() or out_m4a.stat().st_size < 1000:
            yt_tail = yt_err_log.read_text(encoding="utf-8", errors="replace")[-250:] if yt_err_log.is_file() else ""
            err_tail = ((ff_r.stderr or "")[-200:] + (" | yt-dlp: " + yt_tail if yt_tail else "")).strip()
            raise RuntimeError(f"live DVR audio catch-up failed: {err_tail}")
    else:
        cmd = [yt_dlp, "--no-playlist", "-f", "bestaudio/best"]
        if end_s is not None and end_s > 0:
            cmd += ["--download-sections", f"*0-{end_s:.0f}"]
        cmd += ["-o", out_tmpl, url]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        if r.returncode != 0 and "403" in (r.stderr or r.stdout or "") and ("youtube.com" in url or "youtu.be" in url):
            # Fallback for YouTube SABR 403 on default web client: retry with android player_client
            fb_cmd = [yt_dlp, "--no-playlist", "--extractor-args", "youtube:player_client=android", "-f", "bestaudio/best"]
            if end_s is not None and end_s > 0:
                fb_cmd += ["--download-sections", f"*0-{end_s:.0f}"]
            fb_cmd += ["-o", out_tmpl, url]
            r = subprocess.run(fb_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"yt-dlp audio download failed (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    audio = next(
        (p for p in sorted(cache.glob("audio.*")) if p.suffix.lower() in (".m4a", ".webm", ".opus", ".mp3", ".mp4", ".ogg", ".wav")),
        None,
    )
    if audio is None:
        raise RuntimeError(f"yt-dlp audio download produced no audio file in {cache}")
    meta = {
        "source_url": url,
        "audio_path": str(audio.resolve()),
        "title": info.get("title") or audio.stem,
        "creator": _extract_creator(info)[0],
        "creator_handle": _extract_creator(info)[1],
        "duration": float(info.get("duration") or 0.0),
        "width": int(info.get("width") or 1920),
        "height": int(info.get("height") or 1080),
        "heatmap_peaks": _heatmap_peaks(info),
    }
    _atomic(meta_file, meta)
    return audio.resolve(), meta


def _download_video_section(url: str, start_s: float, end_s: float, out_mp4: Path, *, live_from_start: bool = False) -> Path:
    """Download ONLY a targeted 1080p clip window via yt-dlp --download-sections (~25-40 MB per clip)."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise RuntimeError("yt-dlp is not on PATH; required for targeted section downloads")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    out_mp4.unlink(missing_ok=True)
    section = f"*{start_s:.2f}-{end_s:.2f}"
    fmt = (
        "bestvideo[height<=1080][vcodec!^=av01][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
        "bestvideo[height<=1080][vcodec!^=av01][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=1080][vcodec!^=av01]+bestaudio/"
        "best[height<=1080][vcodec!^=av01]/best[vcodec!^=av01]"
    )
    if live_from_start:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH; required for live YouTube DVR slicing")
        dur_s = max(5.0, float(end_s) - float(start_s))
        yt_p = subprocess.Popen(
            [yt_dlp, "--no-playlist", "--live-from-start", "-f", "b[height<=1080]/bv*[height<=1080]+ba/b", "-o", "-", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ff_cmd = [
            ffmpeg, "-y", "-v", "error", "-i", "pipe:0",
            "-ss", f"{start_s:.2f}", "-t", f"{dur_s:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(out_mp4),
        ]
        ff_r = subprocess.run(ff_cmd, stdin=yt_p.stdout, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if yt_p.stdout:
            yt_p.stdout.close()
        yt_p.terminate()
        try:
            yt_p.wait(5)
        except subprocess.TimeoutExpired:
            yt_p.kill()
        if ff_r.returncode != 0 or not out_mp4.is_file() or out_mp4.stat().st_size < 1000:
            raise RuntimeError(f"live DVR section download ({section}) failed: {(ff_r.stderr or '')[-400:]}")
    else:
        r = subprocess.run(
            [yt_dlp, "--no-playlist", "--download-sections", section,
             "-f", fmt, "--merge-output-format", "mp4", "-o", str(out_mp4), url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        )
        if (r.returncode != 0 or not out_mp4.is_file() or out_mp4.stat().st_size < 1000) and "403" in (r.stderr or r.stdout or "") and ("youtube.com" in url or "youtu.be" in url):
            out_mp4.unlink(missing_ok=True)
            r = subprocess.run(
                [yt_dlp, "--no-playlist", "--extractor-args", "youtube:player_client=android",
                 "--download-sections", section, "-f", fmt, "--merge-output-format", "mp4", "-o", str(out_mp4), url],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
            )
        if r.returncode != 0 or not out_mp4.is_file() or out_mp4.stat().st_size < 1000:
            raise RuntimeError(f"yt-dlp section download ({section}) failed: {(r.stderr or r.stdout)[-400:]}")
    return out_mp4.resolve()


def _download_video(url_or_path: str, *, root: Path = OUT) -> tuple[Path, dict]:
    """Download YouTube/Twitch VODs (or resolve a local video path) via VideoDownloaderService."""
    from .engine.services.video_downloader import VideoDownloaderService

    raw = url_or_path.strip()
    local = Path(raw.removeprefix("file://")) if raw.startswith("file://") else Path(raw)
    if local.is_file():
        return local.resolve(), {"source_url": raw, "path": str(local.resolve()), "title": local.stem,
                                 "width": 1920, "height": 1080}

    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    cache = root / "clip_sources" / key
    cache.mkdir(parents=True, exist_ok=True)
    meta_file = cache / "source.json"
    if meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        cached = Path(meta.get("path", ""))
        if cached.is_file() and cached.stat().st_size > 0:
            return cached, meta

    dl = VideoDownloaderService()
    res = asyncio.run(dl.download_video(raw, output_dir=str(cache)))
    video = Path(res.video_path).resolve()
    md = res.metadata
    meta = {
        "source_url": raw,
        "path": str(video),
        "title": getattr(md, "title", None) or video.stem,
        "duration": float(getattr(md, "duration_seconds", 0.0) or 0.0),
        "width": int(getattr(md, "width", 1920) or 1920),
        "height": int(getattr(md, "height", 1080) or 1080),
    }
    _atomic(meta_file, meta)
    return video, meta


TRANSCRIBE_CHUNK_S = 1800  # 30-min chunks keep faster-whisper STFT RAM < ~350 MB even on 24h+ VODs


def _read_pcm_chunk(stream, max_bytes: int) -> bytes:
    """Read up to max_bytes from a pipe, returning an even byte count for int16 samples."""
    parts: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        piece = stream.read(min(remaining, 1024 * 1024))
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
    buf = b"".join(parts)
    if len(buf) % 2:
        buf = buf[:-1]
    return buf


def _register_cuda_dlls() -> None:
    """On Windows, register pip-installed nvidia/*/bin folders so ctranslate2 finds cublas64_12.dll & cudnn."""
    if os.name != "nt":
        return
    import sys
    nv_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not nv_root.is_dir():
        return
    added: list[str] = []
    for bin_dir in sorted(nv_root.glob("*/bin")):
        if bin_dir.is_dir():
            sdir = str(bin_dir)
            try:
                os.add_dll_directory(sdir)
            except (AttributeError, OSError):
                pass
            added.append(sdir)
    if added:
        cur = os.environ.get("PATH", "")
        missing = [d for d in added if d not in cur]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing) + (os.pathsep + cur if cur else "")


def _resolve_whisper_device() -> str:
    """Prefer ctranslate2 CUDA detection directly (avoids false CPU fallback when torch is CPU-only)."""
    _register_cuda_dlls()
    forced = os.environ.get("FACTORY_WHISPER_DEVICE", "").strip().lower()
    if forced in ("cuda", "cpu"):
        return forced
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _fetch_yt_captions(url: str, cache_dir: Path, *, language: str = "en") -> dict | None:
    """Fast-path for YouTube VODs: fetch existing json3 captions in ~2s instead of running full-VOD Whisper."""
    if not any(h in url.lower() for h in ("youtube.com", "youtu.be")):
        return None
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        return None
    cache = cache_dir / "yt_subs.transcript.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("segments"):
                return data
        except (OSError, ValueError):
            pass
    lang = (language or "en").split("-")[0]
    out_tmpl = str(cache_dir / "subs")
    r = subprocess.run(
        [
            yt_dlp, "--no-playlist", "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", f"{lang}.*,{lang}",
            "--sub-format", "json3",
            "-o", out_tmpl, url,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if r.returncode != 0:
        return None
    sub_file = next(iter(sorted(cache_dir.glob("subs*.json3"))), None)
    if sub_file is None:
        return None
    try:
        raw = json.loads(sub_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    segments: list[dict] = []
    for ev in raw.get("events") or []:
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        start_s = float(ev["tStartMs"]) / 1000.0
        dur_s = float(ev.get("dDurationMs") or 2000.0) / 1000.0
        text = "".join(str(x.get("utf8") or "") for x in segs).replace("\n", " ").strip()
        if not text:
            continue
        segments.append({
            "start": round(start_s, 3),
            "end": round(start_s + max(0.2, dur_s), 3),
            "text": text,
            "words": [],
        })
    if not segments:
        return None
    transcript = {"duration": segments[-1]["end"], "segments": segments, "source": "youtube_subs"}
    _atomic(cache, transcript)
    return transcript


def _transcribe_local(
    video: Path,
    *,
    model_size: str = "base",
    language: str | None = None,
    chunk_s: int = TRANSCRIBE_CHUNK_S,
    fast: bool = False,
    progress_cb=None,
) -> dict:
    """Local faster-whisper transcription in bounded-RAM 30-minute chunks.

    Uses ctranslate2 CUDA detection (with automatic CPU fallback if CUDA DLLs are missing) and
    BatchedInferencePipeline + VAD + greedy decoding in `fast=True` mode (~10-30x faster on long VODs).
    """
    suffix = ".fast.transcript.json" if fast else ".words.transcript.json"
    cache = video.with_suffix(suffix)
    if cache.is_file() and cache.stat().st_mtime >= video.stat().st_mtime:
        return json.loads(cache.read_text(encoding="utf-8"))

    import faster_whisper
    from faster_whisper import WhisperModel
    import numpy as np

    lang_arg = None if (not language or language == "auto") else language
    preferred = _resolve_whisper_device()
    devices_to_try = [preferred, "cpu"] if preferred == "cuda" else ["cpu"]

    def _run_on(device: str) -> dict:
        compute_type = "float16" if device == "cuda" else "int8"
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        batched_cls = getattr(faster_whisper, "BatchedInferencePipeline", None)
        use_batched = fast and batched_cls is not None
        runner = batched_cls(model=model) if use_batched else model
        batch_size = 16 if device == "cuda" else 8

        def _transcribe_input(inp):
            kw = {
                "language": lang_arg,
                "beam_size": 1 if fast else 5,
                "word_timestamps": not fast,
                "condition_on_previous_text": False,
                "vad_filter": True,
            }
            if use_batched:
                kw["batch_size"] = batch_size
            return runner.transcribe(inp, **kw)

        ffmpeg = shutil.which("ffmpeg")
        segments: list[dict] = []
        if ffmpeg:
            proc = subprocess.Popen(
                [ffmpeg, "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            chunk_bytes = int(chunk_s) * 16000 * 2
            offset_s = 0.0
            chunk_idx = 0
            try:
                assert proc.stdout is not None
                while True:
                    buf = _read_pcm_chunk(proc.stdout, chunk_bytes)
                    if not buf:
                        break
                    chunk_idx += 1
                    pcm = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    chunk_dur = len(pcm) / 16000.0
                    if progress_cb is not None:
                        progress_cb(chunk_idx, offset_s, chunk_dur, device)
                    segs_iter, _ = _transcribe_input(pcm)
                    for seg in segs_iter:
                        words = (
                            [
                                {
                                    "start": round(offset_s + float(w.start), 3),
                                    "end": round(offset_s + float(w.end), 3),
                                    "word": w.word.strip(),
                                }
                                for w in (seg.words or []) if w.word and w.word.strip()
                            ]
                            if not fast else []
                        )
                        segments.append({
                            "start": round(offset_s + float(seg.start), 3),
                            "end": round(offset_s + float(seg.end), 3),
                            "text": (seg.text or "").strip(),
                            "words": words,
                        })
                    offset_s += chunk_dur
                    del pcm
            finally:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
            duration = round(offset_s, 3) if offset_s > 0 else (segments[-1]["end"] if segments else 0.0)
        else:
            segs_iter, info = _transcribe_input(str(video))
            for seg in segs_iter:
                words = (
                    [
                        {"start": round(float(w.start), 3), "end": round(float(w.end), 3), "word": w.word.strip()}
                        for w in (seg.words or []) if w.word and w.word.strip()
                    ]
                    if not fast else []
                )
                segments.append({
                    "start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                    "text": (seg.text or "").strip(),
                    "words": words,
                })
            duration = float(getattr(info, "duration", 0.0) or (segments[-1]["end"] if segments else 0.0))
        return {"duration": duration, "segments": segments, "device": device}

    last_err: Exception | None = None
    transcript: dict | None = None
    for dev in devices_to_try:
        try:
            transcript = _run_on(dev)
            break
        except (RuntimeError, OSError) as ex:
            last_err = ex
            if dev == "cuda":
                continue
            raise
    if transcript is None:
        raise last_err or RuntimeError("faster-whisper failed on all devices")
    _atomic(cache, transcript)
    return transcript


_STREAMER_REACTION_RE = re.compile(
    r"\b(chat|bro|bruh|dude|no way|oh my god|omg|wtf|what the|let'?s go|wait a second|wait what|"
    r"holy|insane|crazy|clip (?:that|it)|lmao|nah+|yo+|look at (?:this|that)|are you kidding|"
    r"no shot|ain'?t no way|cooked|clutch|gg|stream|twitch|youtube|guys|y'?all)\b",
    re.I,
)


def _lacks_streamer_reaction(
    transcript: dict,
    start_s: float,
    end_s: float,
    audio_spikes: list[dict],
    *,
    min_words_per_s: float = 0.75,
) -> bool:
    """Return True if a candidate looks like quiet gameplay OR pure in-game NPC/cutscene dialogue with
    no audio hype spike and no live streamer reaction cues."""
    dur = max(1.0, float(end_s) - float(start_s))
    has_spike = any((start_s - 2.0) <= float(sp.get("t_s", -999)) <= (end_s + 2.0) for sp in audio_spikes)
    if has_spike:
        return False
    texts: list[str] = []
    word_count = 0
    for seg in transcript.get("segments", []):
        if seg["end"] <= start_s or seg["start"] >= end_s:
            continue
        t = str(seg.get("text") or "")
        if t:
            texts.append(t)
        words = seg.get("words")
        if words:
            word_count += sum(1 for w in words if w["end"] > start_s and w["start"] < end_s)
        else:
            word_count += len(t.split())
    if (word_count / dur) < min_words_per_s:
        return True
    joined = " ".join(texts)
    return not bool(_STREAMER_REACTION_RE.search(joined))


def _slice_segments(transcript: dict, start_s: float, end_s: float, *, offset_s: float = 0.0) -> list[dict]:
    """Slice transcript segments overlapping [start_s, end_s], optionally rebased by offset_s (for sliced VODs)."""
    out = []
    for seg in transcript.get("segments", []):
        if seg["end"] <= start_s or seg["start"] >= end_s:
            continue
        words = [
            {"start": round(max(0.0, w["start"] - offset_s), 3),
             "end": round(max(0.0, w["end"] - offset_s), 3),
             "word": w["word"]}
            for w in seg.get("words", []) if w["end"] > start_s and w["start"] < end_s
        ]
        out.append({
            "start": round(max(0.0, seg["start"] - offset_s), 3),
            "end": round(max(0.0, seg["end"] - offset_s), 3),
            "text": seg["text"],
            "words": words,
        })
    return out


def ingest_vod(
    workspace: str,
    url_or_path: str,
    *,
    num_clips: int | None = None,
    layout: str | None = None,
    caption_style: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    long_vod: bool | None = None,
    ingest_id: str | None = None,
    root: Path = OUT,
    claude_bin: list[str] | None = None,
    sensor: Path | None = SENSOR,
) -> list[dict]:
    """Download source media, transcribe locally via faster-whisper, rank viral highlights
    via the multi-provider LLM router (claude / gemini / openai-compatible), and queue
    one autovideo job per highlight."""
    from .highlights import get_highlights
    from ..llm.router import route_call

    ws = load(workspace)
    if format_of(ws)["pipeline"] != "clip":
        raise ValueError(f"{workspace} uses pipeline {format_of(ws)['pipeline']!r}, not 'clip'")

    cfg = ws.get("clip") or {}
    count = int(num_clips or cfg.get("num_clips", 5))
    chosen_layout = layout or cfg.get("default_layout", "auto")
    chosen_style = caption_style or cfg.get("caption_style", "neon")
    chosen_provider = provider or cfg.get("provider") or os.environ.get("FACTORY_PROVIDER")
    chosen_model = model or cfg.get("model")

    raw = url_or_path.strip()

    vod_title = ""
    creator_name = ""
    creator_handle = ""

    def _prog(
        stage: str,
        detail: str = "",
        *,
        title: str | None = None,
        queued: int = 0,
        progress: int | None = None,
        error: str | None = None,
    ) -> None:
        if ingest_id:
            update_ingest_status(
                ingest_id, channel=workspace, url=raw, stage=stage, detail=detail,
                title=title if title is not None else (vod_title or None),
                queued=queued, progress=progress, error=error, root=root,
            )

    local_file = Path(raw.removeprefix("file://")) if raw.startswith("file://") else Path(raw)
    is_remote = not local_file.is_file()
    _prog("probing", "Reading stream metadata and duration")

    target_url = raw
    is_live_catchup = False
    yt_live_dvr = False
    live_end_s: float | None = None

    if is_remote:
        from .live import twitch_login
        from .radar import twitch_active_vod
        if tw_login := twitch_login(raw):
            active = twitch_active_vod(tw_login)
            if active is None:
                raise RuntimeError(
                    f"Twitch stream '{tw_login}' has no active archive VOD yet; use Radar & Watch Live -> Watch instead."
                )
            target_url = active["vod_url"]
            creator_name = str(active.get("creator") or tw_login)
            creator_handle = str(active.get("creator_handle") or f"@{tw_login}")
            vod_title = f"{creator_name} — {active.get('title') or tw_login}"
            is_live_catchup = True
            live_end_s = float(active.get("duration") or active.get("elapsed_s") or 0.0) or None
            _prog("probing", f"Catch-up mode: live Twitch VOD ({(live_end_s or 0) / 3600:.1f}h so far)", title=vod_title)

    remote_info = _probe_remote_info(target_url) if is_remote else {}
    if remote_info:
        c_name, c_handle = _extract_creator(remote_info)
        if c_name and not creator_name:
            creator_name = c_name
        if c_handle and not creator_handle:
            creator_handle = c_handle
    if remote_info.get("title") and not vod_title:
        raw_t = str(remote_info["title"])
        vod_title = f"{creator_name} — {raw_t}" if (creator_name and creator_name.lower() not in raw_t.lower()) else raw_t
        _prog("probing", "Loaded stream metadata", title=vod_title)

    if is_remote and not is_live_catchup and (remote_info.get("is_live") or remote_info.get("live_status") == "is_live"):
        is_live_catchup = True
        yt_live_dvr = True
        start_ts = remote_info.get("release_timestamp") or remote_info.get("timestamp")
        live_end_s = min(21600.0, max(120.0, time.time() - float(start_ts))) if start_ts else 3600.0
        _prog("probing", f"Catch-up mode: live stream ({live_end_s / 3600:.1f}h so far)", title=vod_title)

    remote_dur = float(remote_info.get("duration") or live_end_s or 0.0)
    use_long_vod = is_remote and (
        True if is_live_catchup else (bool(long_vod) if long_vod is not None else remote_dur > LONG_VOD_THRESHOLD_S)
    )

    if use_long_vod:
        hrs = f" ({remote_dur / 3600:.1f}h)" if remote_dur else ""
        label = "Downloading live catch-up audio" if is_live_catchup else "Downloading audio-only track"
        _prog("downloading", f"{label}{hrs}", title=vod_title)
        media_for_whisper, meta = _download_audio_only(
            target_url,
            remote_info,
            root=root,
            live_from_start=yt_live_dvr,
            end_s=live_end_s if yt_live_dvr else None,
            force_refresh=is_live_catchup,
        )
        video: Path | None = None
    else:
        _prog("downloading", "Downloading source video", title=vod_title)
        video, meta = _download_video(url_or_path, root=root)
        if not vod_title and meta.get("title"):
            vod_title = str(meta["title"])
            _prog("downloading", "Downloaded source video", title=vod_title)
        media_for_whisper = video
        if remote_info and not meta.get("heatmap_peaks"):
            meta["heatmap_peaks"] = _heatmap_peaks(remote_info)

    total_chunks = max(1, math.ceil(remote_dur / TRANSCRIBE_CHUNK_S)) if remote_dur > 0 else 0

    def _whisper_prog(chunk_idx: int, offset_s: float, _chunk_dur: float, device: str = "cpu") -> None:
        if total_chunks > 1:
            pct = 40 + int(32 * min(chunk_idx - 1, total_chunks) / total_chunks)
            _prog(
                "transcribing",
                f"Transcribing chunk {chunk_idx}/{total_chunks} on {device.upper()} ({offset_s / 3600:.1f}h / {remote_dur / 3600:.1f}h)",
                progress=pct,
            )

    transcript = _fetch_yt_captions(target_url, media_for_whisper.parent, language=ws.get("language", "en")) if (use_long_vod and not is_live_catchup) else None
    if transcript is not None:
        _prog("transcribing", f"Loaded YouTube captions ({len(transcript['segments'])} lines)", progress=72)
    else:
        _prog("transcribing", f"Transcribing locally with faster-whisper ({cfg.get('whisper_model', 'base')})")
        transcript = _transcribe_local(
            media_for_whisper,
            model_size=cfg.get("whisper_model", "base"),
            language=ws.get("language", "en"),
            fast=use_long_vod,
            progress_cb=_whisper_prog,
        )

    work = media_for_whisper.parent / "highlights_work"
    work.mkdir(parents=True, exist_ok=True)
    playbook_rel = (ws.get("playbooks") or {}).get("highlights")
    system_file = Path(ws["dir"]) / playbook_rel if playbook_rel else None
    if system_file and not system_file.is_file():
        system_file = None

    if not creator_name and meta.get("creator"):
        creator_name = str(meta.get("creator") or "")
        creator_handle = str(meta.get("creator_handle") or creator_handle)
    audio_spikes = _audio_hype_spikes(media_for_whisper)
    signal_hints = ""
    if creator_name:
        signal_hints += (
            f"\n\n## Streamer / Creator\n{creator_name}"
            + (f" ({creator_handle})" if creator_handle else "")
            + " — use their actual name in highlight titles and hooks instead of generic 'streamer'."
        )
    if peaks := meta.get("heatmap_peaks"):
        signal_hints += (
            "\n\n## Viewer Most-Replayed Peaks (YouTube heatmap — strong signal for visual-only moments)\n"
            + json.dumps(peaks[:12])
        )
    chat_spikes = _vod_chat_spikes(target_url, media_for_whisper.parent, duration_s=remote_dur) if is_remote else []
    if chat_spikes:
        signal_hints += (
            "\n\n## Twitch Chat Explosions (viewers spamming LMAO/KEKW/CLIP — strongest signal for BOTH vocal and silent cam reactions)\n"
            + json.dumps(chat_spikes[:15])
        )
    if audio_spikes:
        signal_hints += (
            "\n\n## Audio hype spikes (loud laughter, screams, sudden chaos — strong signal for peak moments)\n"
            + json.dumps(audio_spikes[:15])
        )

    def highlight_llm(prompt: str) -> str:
        r = route_call(
            "clip:topics",
            prompt + signal_hints if signal_hints and "classify the content type" not in prompt else prompt,
            cwd=work,
            system_file=system_file,
            claude_bin=claude_bin,
            usage_log=work / "usage.jsonl",
            sensor=sensor,
            provider=chosen_provider,
            model=chosen_model,
            legacy_claude_fn=call,
        )
        if r.isolation:
            raise RuntimeError(f"clip highlight selection not isolated: {r.isolation}")
        return r.text

    _prog("scoring", f"Scoring viral highlights via {chosen_provider or 'router'}")
    def _score_prog(done: int, total: int) -> None:
        pct = 72 + int(8 * done / max(1, total))
        _prog("scoring", f"Scoring highlights chunk {done}/{total} via {chosen_provider or 'router'}", progress=pct)

    from ..llm.config import get_step_provider, load_providers_config
    from ..llm.router import _protocol_for
    eff_provider = (chosen_provider or get_step_provider("topics")[0] or "claude").lower()
    all_p_cfg = load_providers_config()
    prov_spec = all_p_cfg.get(eff_provider) if isinstance(all_p_cfg.get(eff_provider), dict) else {}
    proto = _protocol_for(eff_provider, all_p_cfg)
    _default_workers = {
        "cli": 6,
        "claude": 4,
        "openai": 4,
        "gemini": 2,
    }
    workers = int(cfg.get("score_workers") or prov_spec.get("workers") or _default_workers.get(proto, 2))
    score_chunk_s = int(cfg.get("score_chunk_s") or (3600 if use_long_vod else 1200))
    hl_style = str(cfg.get("highlight_style") or ("gaming" if system_file else "default"))
    raw_picks = get_highlights(
        transcript, num_clips=count + 5, llm_fn=highlight_llm, max_workers=workers,
        progress_cb=_score_prog, chunk_size=score_chunk_s, style=hl_style,
    ).get("highlights", [])
    min_wps = float(cfg.get("min_words_per_s", 0.75))
    if bool(cfg.get("require_streamer_reaction", False)):
        reactive = [
            p for p in raw_picks
            if not _lacks_streamer_reaction(transcript, float(p["start_time"]), float(p["end_time"]), audio_spikes, min_words_per_s=min_wps)
        ]
        picks = (reactive or raw_picks)[:count]
    else:
        picks = raw_picks[:count]

    source_key = _canonical_source_key(target_url)
    skipped_dupes = 0
    created = []
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for idx, pick in enumerate(picks, 1):
        start_s, end_s = float(pick["start_time"]), float(pick["end_time"])
        if not _try_reserve_window(workspace, source_key, start_s, end_s, ingest_id or f"ing_{idx}", root=root):
            skipped_dupes += 1
            continue
        title = pick.get("title") or f"Clip {idx}"
        hook = pick.get("hook_sentence") or ""
        reason = pick.get("virality_reason") or ""
        score = float(pick.get("score") or 0)

        brief_md = work / f"clip_{idx:02d}.md"
        brief_md.write_text(
            f"# Clip: {title}\n\nSource: {url_or_path}\nWindow: {start_s:.1f}s - {end_s:.1f}s\n"
            f"Hook: {hook}\nReason: {reason}\n",
            encoding="utf-8",
        )
        jid = jobs.new(workspace, brief_md, root=root)
        d = root / "jobs" / jid
        if use_long_vod:
            slice_start = max(0.0, start_s - SLICE_PAD_S)
            slice_end = min(remote_dur or (end_s + SLICE_PAD_S), end_s + SLICE_PAD_S)
            _prog("slicing", f"Downloading 1080p clip {idx} of {len(picks)}", queued=len(created),
                  progress=80 + int(18 * idx / max(1, len(picks))))
            clip_source = _download_video_section(target_url, slice_start, slice_end, d / "source_slice.mp4", live_from_start=yt_live_dvr)
            rel_start, rel_end, cap_offset = round(start_s - slice_start, 2), round(end_s - slice_start, 2), slice_start
        else:
            assert video is not None
            clip_source, rel_start, rel_end, cap_offset = video, start_s, end_s, 0.0
        clip_caps = _slice_segments(transcript, start_s, end_s, offset_s=cap_offset)
        if use_long_vod and not any(seg.get("words") for seg in clip_caps):
            # Run fast word-level Whisper on just this ~60s clip slice for exact word karaoke & tight pacing
            try:
                slice_tr = _transcribe_local(
                    clip_source, model_size=cfg.get("whisper_model", "base"), language=ws.get("language", "en"), fast=False
                )
                clip_caps = _slice_segments(slice_tr, rel_start, rel_end, offset_s=0.0)
            except Exception:
                pass
        spec = default_spec(
            source_url=url_or_path,
            source_path=str(clip_source),
            source_width=meta.get("width", 1920),
            source_height=meta.get("height", 1080),
            start_s=rel_start,
            end_s=rel_end,
            title=title,
            hook=hook,
            reason=reason,
            score=score,
            layout=chosen_layout,
            caption_style=chosen_style,
            pacing=cfg.get("pacing", "natural"),
            speed=float(cfg.get("speed", 1.0)),
            include_captions=bool(cfg.get("include_captions", True)),
            include_title=bool(cfg.get("include_title", False)),
            captions=clip_caps,
            normalize_audio=bool(cfg.get("normalize_audio", True)),
            allow_profanity=bool(cfg.get("allow_profanity", False)),
            creator=creator_name,
            creator_handle=creator_handle,
            title_stop_words=cfg.get("title_stop_words"),
        )
        _atomic(d / "clip_spec.json", spec)
        _atomic(d / "topic.json", {
            "key": title,
            "score": score,
            "lever": "vod_clip",
            "source_url": url_or_path,
            "creator": creator_name,
            "creator_handle": creator_handle,
        })
        _atomic(d / "review.json", {"block": False, "defects": [], "source": "clip_ingest", "at": at})
        created.append({"job": jid, "title": title, "score": score, "start_s": start_s, "end_s": end_s})
    dupe_note = f" ({skipped_dupes} duplicate skipped)" if skipped_dupes else ""
    _prog("done", f"Queued {len(created)} clip job(s){dupe_note}", queued=len(created))
    return created


def clip_cut(job: jobs.Job) -> dict:
    """Pipeline stage for `format.pipeline == 'clip'`: render final.mp4 via RenderingService
    (YuNet face tracking, smart screen+webcam layouts, tight pacing/dead-air cuts, ASS caption presets)."""
    from .engine.config import get_caption_preset
    from .engine.services.rendering_service import RenderingService, RenderRequest
    from .engine.services.transcription_service import TranscriptSegment, TranscriptWord

    spec_path = job.dir / "clip_spec.json"
    if not spec_path.is_file():
        raise RuntimeError(f"no clip_spec.json in {job.dir}; ingest via `python -m factory clip ingest` or console")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    allow_prof = bool(spec.get("allow_profanity", False))
    segments = [
        TranscriptSegment(
            start_time_ms=int(round(float(seg["start"]) * 1000)),
            end_time_ms=int(round(float(seg["end"]) * 1000)),
            text=clean_caption_text(str(seg.get("text") or ""), allow_profanity=allow_prof),
            words=[
                TranscriptWord(
                    word=clean_caption_text(str(w["word"]), allow_profanity=allow_prof),
                    start_time_ms=int(round(float(w["start"]) * 1000)),
                    end_time_ms=int(round(float(w["end"]) * 1000)),
                )
                for w in seg.get("words", [])
            ],
        )
        for seg in spec.get("captions", [])
    ]

    out_mp4 = job.dir / "final.mp4"
    req = RenderRequest(
        video_path=str(spec["source_path"]),
        output_path=str(out_mp4),
        start_time_ms=int(round(float(spec["start_s"]) * 1000)),
        end_time_ms=int(round(float(spec["end_s"]) * 1000)),
        source_width=int(spec.get("source_width") or 1920),
        source_height=int(spec.get("source_height") or 1080),
        transcript_segments=segments,
        include_captions=bool(spec.get("include_captions", True)),
        caption_style=get_caption_preset(spec.get("caption_style", "neon")),
        title_text=spec.get("hook") or spec.get("title") or None,
        include_title=bool(spec.get("include_title", False)),
        title_stop_words=list(spec.get("title_stop_words") or []),
        aspect_ratio="9:16",
        layout_style=spec.get("layout", "auto"),
        pacing=spec.get("pacing", "natural"),
        video_speed=float(spec.get("speed") or 1.0),
    )

    renderer = RenderingService()
    res = asyncio.run(renderer.render_clip(req))
    normalized = False
    if bool(spec.get("normalize_audio", True)):
        try:
            normalized = _normalize_loudness(out_mp4)
        except Exception:
            normalized = False
    try:
        contact_sheet(out_mp4, job.dir / "sheet.png")
    except Exception:
        pass

    narration = " ".join(str(c.get("text") or "").strip() for c in spec.get("captions", []) if c.get("text")).strip()
    if not narration:
        narration = spec.get("hook") or spec.get("title") or job.id
    (job.dir / "narration.txt").write_text(narration + "\n", encoding="utf-8")
    _atomic(job.dir / "script.json", {
        "narration": narration,
        "hooks": [h for h in (spec.get("hook"), spec.get("title")) if h],
        "source_url": spec.get("source_url", ""),
        "creator": spec.get("creator", ""),
        "creator_handle": spec.get("creator_handle", ""),
    })
    duration_s = (float(res.duration_ms) / 1000.0) if getattr(res, "duration_ms", None) else float(getattr(res, "duration_seconds", 0.0) or (spec["end_s"] - spec["start_s"]))
    return {
        "seconds": round(duration_s, 2),
        "layout": spec.get("layout", "auto"),
        "caption_style": spec.get("caption_style", "neon"),
        "pacing": spec.get("pacing", "natural"),
        "normalized_lufs": normalized,
    }
