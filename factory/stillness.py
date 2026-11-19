"""Nothing on screen stays still for more than 1.5 s, except a closing hold of up to 3.0 s (design 8.5 item 5;
FAILURE_MODES.md, stillness). ffmpeg's freezedetect finds the spans; changes under its -50 dB floor count as still."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

MAX_STILL_S = 1.5
CLOSING_HOLD_S = 3.0  # finding F5's MAX_HOLD_S: a composition may hold its closing line this long
NOISE = "-50dB"  # measured 5 Nov: -60 dB breaks a held frame at each re-encoded keyframe; -45 dB flags slow motion


def _probe(video: Path) -> tuple[float, float]:
    """(duration, frame seconds); raises when there is no readable video stream."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=avg_frame_rate:format=duration", "-of", "json", str(video)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    info = json.loads(r.stdout or "{}")
    if r.returncode != 0 or not info.get("streams"):
        raise RuntimeError(f"{video}: no readable video stream ({r.stderr.strip()[-200:]})")
    num, den = (float(x) for x in info["streams"][0]["avg_frame_rate"].split("/"))
    return float(info["format"]["duration"]), den / num if num else 1 / 30


def still_spans(video: Path, min_s: float = MAX_STILL_S) -> list[tuple[float, float]]:
    duration, _ = _probe(video)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-map", "0:v:0",
                        "-vf", f"freezedetect=n={NOISE}:d={min_s}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"{video}: ffmpeg freezedetect failed: {r.stderr.strip()[-300:]}")
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", r.stderr)]
    return [(s, ends[i] if i < len(ends) else duration) for i, s in enumerate(starts)]  # an open span runs to the end


def check(video: Path, max_hold_s: float = CLOSING_HOLD_S, max_still_s: float = MAX_STILL_S) -> list[dict]:
    """Still spans over max_still_s (a channel's format); a closing span may run to max_hold_s and no further (I6)."""
    duration, frame = _probe(video)
    found = []
    for start, end in still_spans(video, min(max_still_s, max_hold_s)):  # catch a closing hold under max_still_s too
        seconds = end - start
        closing = end >= duration - frame - 1e-6
        if seconds <= (max_hold_s if closing else max_still_s) + 1e-6:  # a closing span answers to the hold limit alone
            continue
        found.append({"start": round(start, 3), "end": round(end, 3), "seconds": round(seconds, 3),
                      "why": f"closing hold over {max_hold_s:g} s" if closing else f"still for over {max_still_s:g} s"})
    return found
