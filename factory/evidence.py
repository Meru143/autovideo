"""Review evidence a model that only sees images can judge (operator, 5 Nov: the review must catch motion and sound,
not only stills): motion from motiscope (MIT; measured cuts, holds, easing and loop, plus keyframes at the moments
that mattered) and sound from earshot (MIT; a listening sheet and a critique against professional launch films).
Everything runs locally, so audio stays on this machine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .audio import EARSHOT_PY

MOTISCOPE = Path(os.environ.get("FACTORY_MOTISCOPE", Path.home() / "Downloads" / "ext" / "motiscope"))
MAX_KEYFRAMES = 12


def _run(cmd: list[str], timeout_s: int = 900) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[min(2, len(cmd) - 1)]).name} failed (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    return r


def motion(video: Path, out_dir: Path) -> dict:
    """motiscope's measurements plus a 6x2 sheet of keyframes: frame 0, its salient moments (key poses, peaks, hold
    starts) and the last frame, so the review sees whether the ending leads back into the opening. motiscope's own
    "loop" means a repeating animation, never a story's ending, and the review read it as one (9 Nov): left out."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-I", str(MOTISCOPE / "scripts" / "analyze_motion.py"), str(video), str(out_dir)])
    m = json.loads((out_dir / "motion.json").read_text(encoding="utf-8"))
    last = round(m["video"]["duration_seconds"] - 1 / m["video"]["fps"], 2)
    salient = sorted({round(s["t"], 2) for s in m.get("salient", [])
                      if s.get("reason") in ("keypose", "peak", "holdstart") and 0 < s["t"] < last})
    step = max(1, len(salient) // (MAX_KEYFRAMES - 2))
    times = [0.0, *salient[::step][:MAX_KEYFRAMES - 2], last]
    frames = []
    for i, t in enumerate(times):
        f = out_dir / f"key_{i:02d}.png"
        seek = (["-sseof", "-0.5", "-i", str(video), "-update", "1"] if t == last  # every frame over one file: the last stays
                else ["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1"])
        _run(["ffmpeg", "-v", "error", "-y", *seek, "-vf", "scale=216:384", str(f)], timeout_s=120)
        frames.append(f)
    while len(frames) < 12:  # tile needs a full grid
        frames.append(frames[-1])
    _run(["ffmpeg", "-v", "error", "-y", *[a for f in frames for a in ("-i", str(f))], "-filter_complex",
          "".join(f"[{i}:v]" for i in range(12)) + "xstack=inputs=12:layout=" + "|".join(
              f"{(i % 6) * 216}_{(i // 6) * 384}" for i in range(12)), str(out_dir / "motion.png")], timeout_s=120)
    holds = [s for s in m.get("segments", []) if s.get("kind") == "hold"]
    longest = max(((s["end_ms"] - s["start_ms"]) / 1000 for s in holds), default=0.0)
    return {"png": str(out_dir / "motion.png"), "summary": {
        "cuts": len(m.get("signal", {}).get("scene_cuts", [])), "segments": len(m.get("segments", [])),
        "longest_hold_s": round(longest, 2), "dominant_easing": m.get("dominant_easing"),
        "keyframe_times_s": times,
        "profile": (m.get("summary", "").split("Profile:")[-1].split(".")[0].strip() if "Profile:" in m.get("summary", "") else "")}}


def critique(video: Path, out_dir: Path) -> dict:
    """earshot's critique against professional launch films, kept in out_dir/critique.json and reused while the film
    is unchanged: the render stage's audio QA and the review's evidence read the same run."""
    out = out_dir / "critique.json"
    if not (out.exists() and out.stat().st_mtime >= video.stat().st_mtime):
        out_dir.mkdir(parents=True, exist_ok=True)
        py = str(Path(os.environ.get("FACTORY_EARSHOT_PY", EARSHOT_PY)))
        _run([py, "-I", "-m", "earshot", "critique", str(video), "--json", "--out", str(out)])
    return json.loads(out.read_text(encoding="utf-8"))


def sound(video: Path, out_dir: Path, cues: list[float]) -> dict:
    """earshot's listening sheet (frames, labelled sound events, spectrogram, loudness, sync to our declared events)
    and its critique against professional launch films."""
    py = str(Path(os.environ.get("FACTORY_EARSHOT_PY", EARSHOT_PY)))
    out_dir.mkdir(parents=True, exist_ok=True)
    cue_arg = ["--cues", ",".join(f"{t:.3f}" for t in cues)] if cues else []
    _run([py, "-I", "-m", "earshot", "sheet", str(video), "-o", str(out_dir / "sound.png"), *cue_arg])
    c = critique(video, out_dir)
    return {"png": str(out_dir / "sound.png"), "critique": c.get("summary") or c.get("verdict") or json.dumps(c)[:1500]}
