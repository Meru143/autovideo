"""Renderer wrapper (design 6a; Plan 2b Tasks 7-8), ported from spikes/bakeoff/bakeoff/render.py and frames.py: the
pinned HyperFrames 0.9.9 install, a localhost GSAP mirror, `check` with its layout pass at every tween boundary (art
over text fails as text_occluded), a picture-only render, a mux with BT.709 tags, and a contact sheet."""
from __future__ import annotations

import contextlib
import functools
import http.server
import json
import math
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import ROOT
from .claude_call import HARDENED
from .procs import RunResult, run_cli

BAKEOFF = ROOT / "spikes" / "bakeoff"  # the pinned install and vendor mirror the bake-off proved (Plan 2b)
VENDOR = BAKEOFF / "vendor"
MIRROR_PORT = 8766  # the bake-off's own mirror uses 8765
MIRROR_URL = f"http://127.0.0.1:{MIRROR_PORT}/"
# recorded in the report, never blocking: audio is muxed after the render (6a); and a long file only reads badly to a
# person, while ours are written per video and never hand-edited (story-test C, 5 Nov: $5.25 of fixes spent on it)
ALLOWED = {"film_silent", "composition_file_too_large"}
CAPTION_ZONE = "x0=0;y0=0.65;x1=1;y1=1"  # 9:16: text stays above y 1248 (5.7 check 12); the band is the platform's
CHECK_TIMEOUT_S, RENDER_TIMEOUT_S = 900, 7200


# Both ways HyperFrames hides an element from its checks; a video's checks must see everything a viewer sees
HIDDEN_FROM_CHECKS = re.compile(r"data-layout-ignore|data-layout-check\s*=\s*[\"']?ignore")


class CheckFailed(RuntimeError):
    def __init__(self, findings: list[dict], report: dict):
        super().__init__(f"{len(findings)} check finding(s): {sorted({f.get('code', '?') for f in findings})}")
        self.findings, self.report = findings, report


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def vendor_mirror():
    """Serve the vendor folder at the npm-mirror root HYPERFRAMES_GSAP_BASE_URL points at (127.0.0.1 only).
    Uses an OS-assigned ephemeral port so parallel check/render jobs never collide on a hardcoded port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(VENDOR)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def _env(mirror_url: str | None = None) -> dict[str, str]:
    import os
    return {**os.environ, **HARDENED, "HYPERFRAMES_GSAP_BASE_URL": mirror_url or MIRROR_URL}


def _hf() -> list[str]:
    paths = json.loads((BAKEOFF / "paths.json").read_text(encoding="utf-8"))
    return [paths["hf_node"], str(BAKEOFF / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs")]


def _run(cmd: list[str], *, cwd: Path, timeout_s: int, mirror_url: str | None = None) -> RunResult:
    """One run through procs.run_cli: the whole tree dies on a timeout, and Windows' transient spawn EBUSY (a locked
    file) is retried 3 times, never a real failure."""
    return run_cli(cmd, cwd=cwd, env=_env(mirror_url), timeout_s=timeout_s, ebusy_retries=3)


def install_vendor(project: Path) -> None:
    """GSAP and three.js beside the composition: its own script tags aren't rewritten by the mirror (bake-off F4)."""
    target = project / "vendor"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(VENDOR / "gsap@3.15.0" / "dist" / "gsap.min.js", target / "gsap.min.js")
    shutil.copyfile(VENDOR / "three@0.160.1" / "build" / "three.module.js", target / "three.module.js")


def check(project: Path, *, aspect: str = "9:16") -> dict:
    """Lint, runtime, layout (sampled at every tween start and end), motion and contrast; raises CheckFailed."""
    hidden = [f.name for f in project.glob("*.html") if HIDDEN_FROM_CHECKS.search(f.read_text(encoding="utf-8"))]
    if hidden:  # the bake-off's capture probe hid this way; the checks must see everything a viewer sees
        raise CheckFailed([{"code": "measurement_probe", "severity": "error", "category": "lint",
                            "message": f"an element hidden from the layout checks (data-layout-ignore) in {hidden}: "
                                       "remove it; no measurement or debug element belongs in a video"}], {})
    install_vendor(project)
    zone = ["--caption-zone", CAPTION_ZONE] if aspect == "9:16" else []
    with vendor_mirror() as mirror_url:
        r = _run([*_hf(), "check", str(project), "--json", "--strict", "--at-transitions", "--frame-check", *zone],
                 cwd=project, timeout_s=CHECK_TIMEOUT_S, mirror_url=mirror_url)
    if r.timed_out:  # its tree is dead; a check that can't finish is the composition's to lighten
        raise CheckFailed([{"code": "check_timeout", "severity": "error", "category": "runtime",
                            "message": f"the check didn't finish in {CHECK_TIMEOUT_S} s: lighten the heaviest scene "
                                       "(fewer elements, fewer tweens at once)"}], {})
    try:
        report = json.JSONDecoder().raw_decode(r.stdout[r.stdout.index("{"):])[0]
    except ValueError:
        raise RuntimeError(f"check printed no JSON (exit {r.returncode}): {(r.stdout + r.stderr)[-400:]}") from None
    fails = [{**f, "category": cat} for cat, block in report.items()
             if isinstance(block, dict) and isinstance(block.get("findings"), list) for f in block["findings"]
             if f.get("severity") in ("error", "warning") and f.get("code") not in ALLOWED]
    if report.get("browserSkipped"):  # layout, motion and contrast unchecked is not a pass
        fails.insert(0, {"code": "browser_skipped", "severity": "error", "category": "browser",
                         "message": "check skipped its browser session"})
    if fails:
        raise CheckFailed(fails, report)
    return report


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_entries",
                        "stream=codec_type,width,height,r_frame_rate,nb_read_packets,sample_rate:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {r.stderr[-300:]}")
    return json.loads(r.stdout)


def render(project: Path, out_mp4: Path, *, workers: str = "auto", expected_s: float | None = None) -> dict:
    install_vendor(project)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    out_mp4.unlink(missing_ok=True)
    t0 = time.perf_counter()
    from .gpu import gpu_lock  # one heavy GPU job at a time, across the videos running side by side (9 Nov)
    with gpu_lock(), vendor_mirror() as mirror_url:
        r = _run([*_hf(), "render", "--output", str(out_mp4), "--workers", workers, "--fps", "30", "--quality", "looks",
                  # --strict, not --strict-all: check() already gates warnings with ALLOWED; the render stops on errors
                  "--no-best-effort", "--strict", "--browser-gpu"], cwd=project, timeout_s=RENDER_TIMEOUT_S,
                 mirror_url=mirror_url)
    if r.timed_out:
        raise RuntimeError(f"render timed out after {RENDER_TIMEOUT_S} s; its process tree was killed")
    if r.returncode != 0 or not out_mp4.is_file() or out_mp4.stat().st_size < 1000:  # never trust the exit code alone
        raise RuntimeError(f"render failed (exit {r.returncode}): {r.stderr[-600:]}")
    info = probe(out_mp4)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise RuntimeError(f"{out_mp4.name} has no video stream")
    frames, seconds = int(video.get("nb_read_packets", 0)), float(info["format"]["duration"])
    if expected_s is not None and (abs(seconds - expected_s) > 1 / 30 + 1e-6 or frames != round(expected_s * 30)):
        raise RuntimeError(f"partial render: {frames} frames / {seconds:.3f} s, expected {round(expected_s * 30)}")
    return {"seconds": seconds, "frames": frames, "wall_s": round(time.perf_counter() - t0, 1), "workers": workers}


def mux(video: Path, audio_wav: Path, out_mp4: Path) -> Path:
    vd, ad = float(probe(video)["format"]["duration"]), float(probe(audio_wav)["format"]["duration"])
    if abs(vd - ad) > 0.1:  # 5.7 check 2
        raise RuntimeError(f"video {vd:.3f} s and audio {ad:.3f} s differ by more than 0.1 s")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio_wav), "-map", "0:v:0", "-map",
                    "1:a:0", "-c:v", "copy", "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
                    "matrix_coefficients=1:video_full_range_flag=0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-movflags", "+faststart", "-map_metadata", "-1", str(out_mp4)], check=True, timeout=900)
    return out_mp4


def contact_sheet(mp4: Path, out_png: Path, cols: int = 5, rows: int = 2) -> Path:
    """One still per beat-ish slot, tiled, in the video's own aspect (216x384 tiles for 9:16)."""
    info = probe(mp4)
    seconds = float(info["format"]["duration"])
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = int(video["width"]), int(video["height"])
    short = round(384 * min(w, h) / max(w, h) / 2) * 2
    tw, th = (384, short) if w >= h else (short, 384)
    tile = f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-vf",
                    f"fps={cols * rows}/{seconds:.3f},{tile},tile={cols}x{rows}", "-frames:v", "1", str(out_png)],
                   check=True, timeout=300)
    return out_png


STRIP_PAD = "0xff00ff"  # magenta, a colour no scene uses: the review is told such tiles are padding, not frames


def strips(mp4: Path, folder: Path, cols: int = 5) -> list[Path]:
    """One frame a second, in order, as two strips (strip-1.png, strip-2.png), one per half of the film, cols wide at
    216x384 (384x216 for a wide video): the review's look at every second, where the contact sheet shows ten (engine
    Task 6). Unused tiles are STRIP_PAD."""
    info = probe(mp4)
    frames = max(1, math.ceil(float(info["format"]["duration"])))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    tw, th = (384, 216) if int(video["width"]) >= int(video["height"]) else (216, 384)
    tile = f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2"
    first = math.ceil(frames / 2)
    out = []
    for n, (start, count) in enumerate(((0, first), (first, frames - first)), 1):
        rows = max(1, math.ceil(count / cols))
        png = folder / f"strip-{n}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(max(count, 1)), "-i", str(mp4),
                        "-vf", f"fps=1,{tile},tile={cols}x{rows}:color={STRIP_PAD}", "-frames:v", "1", str(png)],
                       check=True, timeout=300)
        out.append(png)
    return out


LEFT_FRAMES = 8  # full-size frames for the findings the checks left (ruling E12): deepest or longest first


def left_frames(mp4: Path, folder: Path, found: list[dict]) -> None:
    """One full-size frame (left-1.png, ...) at the middle of each finding's start-end span, deepest or longest first,
    at most LEFT_FRAMES; the file name goes on the finding as "frame". One frame a second can miss a clip. Old left-*.png
    are removed first. A finding without a span, or over the cap, gets none."""
    for old in folder.glob("left-*.png"):
        old.unlink()
    timed = sorted((f for f in found if "start" in f and "end" in f),
                   key=lambda f: (f.get("depth_cm", 0), f["end"] - f["start"]), reverse=True)
    for n, f in enumerate(timed[:LEFT_FRAMES], 1):
        f["frame"] = f"left-{n}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{(f['start'] + f['end']) / 2:.3f}", "-i", str(mp4),
                        "-frames:v", "1", str(folder / f["frame"])], check=True, timeout=120)
