"""A new picture at least every 5 s (design 8.5 item 4; Plan 2b decision 3; FAILURE_MODES.md, cadence). A new picture
is a cut (ffmpeg scene score over 0.1) or a big change: over 15% of a 16x16 tile grid differing from the frame 3 s
earlier. Small changes on one layout (a ticking counter, word captions) don't count; the stillness check covers
frozen frames and this check covers the same picture held too long."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

MAX_GAP_S = 5.0
FPS, W, H, GRID = 2, 64, 112, 16  # 2 frames a second, downscaled to 64x112 grey: tiles of 4x7 pixels
LOOKBACK_S, TILE_DELTA, TILE_SHARE, SCENE = 3.0, 10.0, 0.15, 0.1


def _duration(video: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError(f"{video}: no readable video stream ({r.stderr.strip()[-200:]})") from None


def picture_times(video: Path) -> list[float]:
    """Times of every new picture: scene cuts plus big tile changes against the frame LOOKBACK_S earlier."""
    duration = _duration(video)
    cuts = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-vf",
                           f"select='gt(scene,{SCENE})',showinfo", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", cuts.stderr)]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-vf",
                          f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=1800)
    if raw.returncode != 0:
        raise RuntimeError(f"{video}: decoding failed: {raw.stderr.decode('utf-8', 'replace')[-200:]}")
    frames = np.frombuffer(raw.stdout, dtype=np.uint8)
    frames = frames[: frames.size // (W * H) * W * H].reshape(-1, H, W).astype(np.float32)
    tiles = frames.reshape(len(frames), GRID, H // GRID, GRID, W // GRID).mean(axis=(2, 4))
    back = int(LOOKBACK_S * FPS)
    for i in range(back, len(tiles)):
        if np.mean(np.abs(tiles[i] - tiles[i - back]) > TILE_DELTA) > TILE_SHARE:
            times.append(i / FPS)
    return sorted(t for t in times if 0 <= t <= duration)


def check(video: Path, max_gap_s: float = MAX_GAP_S) -> list[dict]:
    """Every stretch longer than MAX_GAP_S between new pictures, counting the start and the end as edges."""
    duration = _duration(video)
    edges = [0.0, *picture_times(video), duration]
    found = []
    for start, end in zip(edges, edges[1:]):
        if end - start > max_gap_s + 1e-6:
            found.append({"start": round(start, 2), "end": round(end, 2), "seconds": round(end - start, 2),
                          "why": f"no new picture for over {max_gap_s:g} s"})
    return found
