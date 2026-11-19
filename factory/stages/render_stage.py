"""draft/final render, the early visual checks, the music bed and the master (design 5.5.2 steps 11-13, 5.7, 6b, 8.5
items 5-7 and 12; Plan 2b Task 8, Plan 2c Task 5; FAILURE_MODES.md, stillness, cadence, music). The render's visual
findings go back to the scene writer as one fix from the shared budget; the music writer's bed is mixed under the voice
(re-rendered without a call if the film's length changed); the audio QA fails the stage."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .. import OUT, checkpass
from ..audio import APEX_OFFSET_S, SWELL_DB, declared_events, earshot_sfx, fit_narration, loudness, mix_master
from ..cadence import check as cadence_check
from ..claude_call import SENSOR
from ..evidence import critique
from ..jobs import Job
from ..music.render import render_bed
from ..render import contact_sheet, mux, left_frames, render, strips
from ..stillness import check as stillness_check
from ..workspace import format_of, load
from ..sfx import library
from ..sfx.cues import load_cues
from .music import MIN_MUSIC_FRAC, bed_findings, build_brief
from .scene import fix

LUFS, LUFS_TOL, MAX_TP = -14.0, 1.0, -1.0  # 5.7 check 4 on the encoded file
MIN_SPEECH_OVER_BED_LU = 10.0  # 6b, now the quietest sentence
MAX_MONO_LOSS_DB = 6.0  # the bed alone: a 0.5/0.5 fold of uncorrelated stereo already loses ~3 dB (review I1)
TEMPO_TOL = 0.04  # earshot's tempo against the declared BPM, or its half, double, 2/3 or 3/2 (advisory)
MAX_CUES_OVER_SPEECH_LU = -6.0  # the sound effects' loudest 400 ms during speech, against the voice (Plan 2d)
# Visual findings (a still span, a slow cut) are mechanical: one fix round, the loop's own (6 Nov), then what's left goes
# on, recorded, to the mix and the review (operator, 11 Nov: fix rounds were a fifth of all Claude cost)
RENDER_FIXES = 1


def first_frame_blank(video: Path) -> bool:
    """Frame 0 is a flat field (5.7 check 6): nothing on screen at the first moment."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-frames:v", "1", "-vf",
                          "scale=64:112,format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.uint8).std() < 3.0 if raw else True


def visual_findings(film: Path, fmt: dict) -> list[dict]:
    found = [{"code": "first_frame_blank", "message": "frame 0 is empty; open mid-action with the hook on screen"}] \
        if first_frame_blank(film) else []
    found += [{"code": "still", "start": f["start"], "end": f["end"], "message": f"{f['start']}-{f['end']} s: {f['why']}"} for f in stillness_check(film, fmt["max_hold_s"], fmt["max_still_s"])]
    found += [{"code": "same_picture", "start": f["start"], "end": f["end"], "message": f"{f['start']}-{f['end']} s: {f['why']}; cut or re-frame every 3-5 s"}
              for f in cadence_check(film, fmt["max_gap_s"])]
    return found


def _bed(job: Job, film_s: float) -> tuple[Path, bool]:
    """The music writer's bed at the film's length: (bed.wav, re-rendered?)."""
    folder = job.dir / "music"
    if not (folder / "score.js").is_file():
        raise RuntimeError("no music score in the job: the music stage runs before the render (Plan 2c)")
    report = folder / "render.json"
    if report.exists() and abs(json.loads(report.read_text(encoding="utf-8"))["seconds"] - film_s) <= 0.05:
        return folder / "bed.wav", False
    brief = {**build_brief(job), "duration_s": film_s}  # a repair changed the length: the same score, no call
    (folder / "brief.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")
    if found := bed_findings(render_bed(folder, duration_s=film_s)):  # the same checks as the music stage (I3)
        raise RuntimeError(f"the bed re-rendered at {film_s} s fails its checks: {found}")
    return folder / "bed.wav", True


def stage_cues(job: Job, ws: dict, film_s: float) -> tuple[list, list[dict], str]:
    """(cues, a record of them, 'writer' or 'palette'). The sound writer's sheet, already checked by the music stage,
    resolved through the library (a file missing on this machine is fetched again by id); with no sheet, one palette
    sound on each declared event, as before Plan 2d."""
    try:
        sheet = load_cues(job.dir / "music")
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"music/sfx.json is malformed at render time: {e}") from None
    if sheet:
        kept = [c for c in sheet if float(c["t"]) < film_s]  # a repair may have shortened the film
        return ([{**c, "path": library.path(c["id"]), "start": float(c["t"])} for c in kept],
                [{k: c.get(k) for k in ("id", "t", "gain_db", "serves")} for c in kept], "writer")
    palette, cues, placed = ws["sfx"], [], []  # event kind -> earshot bank id: workspace data the operator can swap
    for kind, t in declared_events(job.dir / "project" / "index.html"):
        if kind not in palette or t >= film_s:
            continue
        wav, peak = earshot_sfx(palette[kind], OUT / "sfx_cache")
        start = max(0.0, t + APEX_OFFSET_S.get(kind, 0.0) - peak)  # the sound's loudest moment lands on its event
        cues.append((wav, start))
        placed.append({"kind": kind, "id": palette[kind], "event_s": t, "start_s": round(start, 3)})
    return cues, placed, "palette"


def _tempo_matches(measured, declared) -> bool | None:
    if not measured or not declared:
        return None
    return any(abs(measured / (declared * k) - 1) <= TEMPO_TOL for k in (1, 2, 0.5, 1.5, 2 / 3))


def _ledger(job: Job, meta: dict) -> None:
    """One line per job, after the QA passes: the music writer's brief.recent reads it (variation, 6b)."""
    ledger = job.root / "music_ledger.jsonl"
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()] if ledger.exists() else []
    if any(r.get("job") == job.id for r in rows):
        return
    score = (job.dir / "music" / "score.js").read_bytes()
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"workspace": job.workspace, "job": job.id, "meta": meta,
                            "score_sha256": hashlib.sha256(score).hexdigest(),
                            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")


def render_checked(job: Job, *, workers: str = "auto", claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    """The render with its checks and its one fix round: {meta, visual_left, check, check_left, rounds}. An engine
    film's check pass comes first (engine Task 4): its clips, missing characters and undefined assets take the same
    fix round as the visual findings. What either leaves after the round goes on, recorded, to the mix and the review
    (ruling E9, operator 11 Nov). A `missing` character never takes the fix round, only the review's eye: an insert the
    film draws with its own code reads as missing (final review C). A check pass that crashes (a film.js that throws
    under node, where the browser may be fine) is one check_crash finding for the round, then "crashed: <error>" and
    the render goes on: only a failed render fails the stage (final review B)."""
    project, film, ws = job.dir / "project", job.dir / "film.mp4", load(job.workspace)
    rounds, check, check_left = 0, checkpass.status(project), []
    while True:
        if check and not check.startswith("skipped"):  # None: not an engine film
            try:
                check_left = checkpass.findings(checkpass.run(project))
                check = "left" if check_left else "clean"
                to_fix = [f for f in check_left if f["code"] != "missing"]
            except RuntimeError as e:
                check_left, check = [], f"crashed: {str(e).splitlines()[0]}"
                to_fix = [{"code": "check_crash", "message": f"the check pass (film.js stepped under node, no browser, "
                                                             f"E.S.ctx null) crashed: {e}"}]
            if to_fix and rounds < RENDER_FIXES:
                fix(job, to_fix, spend=None, claude_bin=claude_bin, sensor=sensor)
                rounds += 1
                continue
        meta = render(project, film, workers=workers)
        findings = visual_findings(film, format_of(ws))
        if not findings or rounds >= RENDER_FIXES:
            break
        fix(job, findings, spend=None, claude_bin=claude_bin, sensor=sensor)
        rounds += 1
    return {"meta": meta, "visual_left": findings, "check": check, "check_left": check_left, "rounds": rounds}


def render_stage(job: Job, *, workers: str = "auto", claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    film, ws = job.dir / "film.mp4", load(job.workspace)
    r = render_checked(job, workers=workers, claude_bin=claude_bin, sensor=sensor)
    meta, findings, rounds = r["meta"], r["visual_left"], r["rounds"]
    hold = fit_narration(job.dir / "narration.wav", meta["seconds"], job.dir / "narration_fit.wav",
                         max_hold_s=format_of(ws)["max_hold_s"])
    bed, rerendered = _bed(job, meta["seconds"])
    timing = json.loads((job.dir / "timing.json").read_text(encoding="utf-8"))
    cues, placed, source = stage_cues(job, ws, meta["seconds"])
    mixed = mix_master(job.dir / "narration_fit.wav", bed, cues, job.dir / "master.wav",
                       speech_spans=[(s["start"], s["end"]) for s in timing["sentences"]],
                       swell_db=ws["music"].get("swell_db", SWELL_DB))
    final = mux(film, job.dir / "master.wav", job.dir / "final.mp4")
    lufs, tp = loudness(final)
    heard = {m["metric"]: m.get("value") for m in critique(final, job.dir / "evidence" / "sound").get("metrics", [])}
    frac, music = heard.get("music_frac"), json.loads((job.dir / "music" / "render.json").read_text(encoding="utf-8"))["meta"]
    qa = [f"integrated {lufs} LUFS is outside {LUFS} ±{LUFS_TOL}"] * (abs(lufs - LUFS) > LUFS_TOL)
    qa += [f"true peak {tp} dBTP is over {MAX_TP}"] * (tp > MAX_TP)
    qa += [f"the quietest sentence sits {mixed['speech_to_bed_lu']} LU over the bed, under {MIN_SPEECH_OVER_BED_LU}"] * (
        mixed["speech_to_bed_lu"] < MIN_SPEECH_OVER_BED_LU)
    qa += [f"the bed's mono fold-down loses {mixed['mono_loss_db']} dB"] * (mixed["mono_loss_db"] >= MAX_MONO_LOSS_DB)
    over = mixed.get("loudest_cue_over_speech_lu")
    qa += [f"sound effects reach {over} LU against the voice during speech, over {MAX_CUES_OVER_SPEECH_LU}"] * (
        over is not None and over > MAX_CUES_OVER_SPEECH_LU)
    qa += [f"earshot hears music in {frac} of its 5 s windows, under {MIN_MUSIC_FRAC}"] * (
        not isinstance(frac, (int, float)) or frac < MIN_MUSIC_FRAC)
    if qa:  # deterministic from here: a miss is the mix's or the bed's, for the operator, never a silent pass
        raise RuntimeError(f"audio QA failed on the encoded file: {qa}")
    _ledger(job, music)
    contact_sheet(final, job.dir / "sheet.png")
    strips(final, job.dir)
    # what the checks still found after their fix round, beside the pictures of this render: the review judges them,
    # each timed one with its own full-size frame (ruling E12)
    left_frames(final, job.dir, r["check_left"] + findings)
    (job.dir / "render_left.json").write_text(json.dumps({"check_left": r["check_left"], "visual_left": findings},
                                                         indent=2), encoding="utf-8")
    return {"seconds": meta["seconds"], "frames": meta["frames"], "render_wall_s": meta["wall_s"], "fix_rounds": rounds,
            "visual_left": findings, "check": r["check"], "check_left": r["check_left"], "hold_s": hold, "sfx": placed, "sfx_source": source, "lufs": lufs, "true_peak": tp,
            "music": {**{k: music.get(k) for k in ("key", "mode", "bpm", "progression", "mood")},
                      "palette": json.loads((job.dir / "music" / "brief.json").read_text(encoding="utf-8"))["palette"].get("palette"),
                      "music_frac": frac, "tempo": heard.get("tempo"), "tempo_matches": _tempo_matches(heard.get("tempo"), music.get("bpm")),
                      **mixed, "rerendered": rerendered}}
