"""The sound writer (Plan 2c Task 4, Plan 2d; design 8.5 items 12 and 21; FAILURE_MODES.md, music and sound): Claude
composes the bed in Tone.js and places sound effects in sfx.json, with allowlisted commands (render, listen, and sfx
search, get, forge, match), directed by the harness guide and the workspace's music playbook. Code then checks the bed
and the cue sheet as it checks a scene; findings go to a fix call from the shared regeneration budget."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from .. import ROOT
from ..audio import declared_events, fit_narration, root_duration
from ..claude_call import SENSOR, call
from ..jobs import Job
from ..music.listen import listen
from ..music.render import RenderFailed, render_bed
from ..sfx.cues import check_cues
from ..workspace import format_of, load

GUIDE = ROOT / "factory" / "music" / "GUIDE.md"
TOOLS = ("Read", "Write", "Edit", "Bash")
_TOOL = f"{Path(sys.executable).as_posix()} -I {(ROOT / 'factory' / 'music' / 'tool.py').as_posix()}"
RENDER_CMD, LISTEN_CMD = f"{_TOOL} render", f"{_TOOL} listen"
SFX_CMDS = {k: f"{_TOOL} sfx {k}" for k in ("search", "get", "forge", "match")}  # Plan 2d: the sound writer's tools
ALLOW = (f"Bash({RENDER_CMD}:*)", f"Bash({LISTEN_CMD}:*)",
         *(f"Bash({c}:*)" for c in SFX_CMDS.values()))  # absolute paths outside the writer's folder
SFX_HELP = ("Sound effects are yours too: only motivated moments get one, and silence is a tool.\n"
            f"{SFX_CMDS['search']} <words>   (the library, earshot's bank, then public-domain Openverse)\n"
            f"{SFX_CMDS['get']} <id>   (takes a result into the library; a cue may only name a library id)\n"
            f"{SFX_CMDS['forge']} <recipe> <target words>   (makes one in code: click, pop, ui_pop, whoosh, swish, "
            "impact, cuthit, riser, sting, notify, typing, shutter, knock, drip, toggle, zip, shimmer)\n"
            f"{SFX_CMDS['match']} <id> <description>   (how well a sound fits a description)\n"
            "Write sfx.json as {\"cues\": [{\"id\", \"t\", \"gain_db\", \"serves\", optional fade_in_s, fade_out_s, "
            "pitch_st, loop_s}]} and listen to the whole mix. A sound with no recipe can be synthesised in score.js.")
MIN_GAP_S, RECENT, MAX_SILENCE_S, MAX_PEAK_DBFS = 0.6, 10, 1.5, -1.0
MIN_MUSIC_FRAC = 0.62  # earshot's reference floor: checked here, so a deaf bed goes back to the writer


def _norm(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def _roles(sentences: list[dict], sign_off: str) -> list[str]:
    """hook (the first), rehook (a later question), signoff (the sign-off's sentences) or, with no sign-off, close (the
    last sentence: the send trigger, where the music settles and loops; Plan 2c final review I7), payoff (the last
    claim before either), claim (the rest)."""
    sign = {_norm(s) for s in re.split(r"(?<=[.?!])\s+", sign_off) if s.strip()}
    roles = ["hook" if i == 0 else "signoff" if _norm(s["text"]) in sign else
             "rehook" if s["text"].rstrip("\"'”’").endswith("?") else "claim" for i, s in enumerate(sentences)]
    if not sign and len(roles) > 1:
        roles[-1] = "close"
    end = roles.index("signoff") if "signoff" in roles else len(roles) - (roles[-1] == "close")
    for i in range(end - 1, 0, -1):
        if roles[i] == "claim":
            roles[i] = "payoff"
            break
    return roles


def _gaps(words: list[dict], duration: float) -> list[list[float]]:
    edges = [(0.0, words[0]["start"]), *((a["end"], b["start"]) for a, b in zip(words, words[1:])),
             (words[-1]["end"], duration)]
    return [[round(a, 3), round(b, 3)] for a, b in edges if b - a >= MIN_GAP_S]


def _recent(job: Job) -> list[dict]:
    ledger = job.root / "music_ledger.jsonl"  # written by the render stage once a bed passes its QA
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()] if ledger.exists() else []
    return [r["meta"] for r in rows if r.get("workspace") == job.workspace][-RECENT:]


def build_brief(job: Job) -> dict:
    ws, index = load(job.workspace), job.dir / "project" / "index.html"
    timing = json.loads((job.dir / "timing.json").read_text(encoding="utf-8"))
    duration = root_duration(index)
    roles = _roles(timing["sentences"], ws.get("sign_off", ""))
    return {"duration_s": duration, "seed": int(hashlib.sha256(job.id.encode()).hexdigest()[:8], 16),
            "narration_s": timing["audio_duration_s"],
            "sentences": [{**s, "role": r} for s, r in zip(timing["sentences"], roles)],
            "gaps": _gaps(timing["words"], duration),
            "events": [{"kind": k, "t": t} for k, t in declared_events(index)],
            "words": [{"start": round(w["start"], 3), "end": round(w["end"], 3)} for w in timing["words"]],
            "palette": ws["music"], "recent": _recent(job)}


# The bed alone, folded to mono as a phone speaker plays it (9 Nov: a StereoWidener at 0.85 measured 4.85 dB here and
# failed the render stage's 6 dB QA on the mixed film, after the whole film was made; the beds that passed measured
# 2.24 dB or less). Caught here, the music writer fixes it in its own loop.
MAX_BED_MONO_LOSS_DB = 4.0


def bed_findings(r: dict) -> list[dict]:
    """The rendered bed's own checks, shared with the render stage's re-render (Plan 2c final review I3)."""
    found = []
    if r.get("mono_loss_db", 0) >= MAX_BED_MONO_LOSS_DB:
        found.append({"code": "mono_fold", "message": f"the bed loses {r['mono_loss_db']} dB folded to mono, as on a "
                      "phone speaker: its left and right cancel. Keep StereoWidener at 0.5 (neutral) or below, and never "
                      "invert one side"})
    if r["peak_dbfs"] > MAX_PEAK_DBFS:
        found.append({"code": "clipping", "message": f"peak {r['peak_dbfs']} dBFS: leave the mixer headroom (≤ -3)"})
    for a, b in r["silent_spans"]:
        if a == 0:
            found.append({"code": "silent_start", "message": f"silent from 0 to {b} s: the music starts at frame 0"})
        elif b - a > MAX_SILENCE_S:
            found.append({"code": "silence", "message": f"silent from {a} to {b} s: keep the bed going"})
    return found


def _check(folder: Path, brief: dict) -> list[dict]:
    (folder / "brief.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")  # the writer may have edited it (I3)
    try:
        r = render_bed(folder)
    except RenderFailed as e:
        return [{"code": "render_failed", "message": str(e)}]
    meta, pal, found = r["meta"], brief["palette"], []
    if not pal["bpm"][0] <= meta["bpm"] <= pal["bpm"][1]:
        found.append({"code": "palette", "message": f"meta.bpm {meta['bpm']} is outside the palette's {pal['bpm']}"})
    if meta["mode"] not in pal["modes"]:
        found.append({"code": "palette", "message": f"meta.mode {meta['mode']!r} isn't one of {pal['modes']}"})
    if any(m.get("key") == meta["key"] and m.get("progression") == meta["progression"] for m in brief["recent"]):
        found.append({"code": "repeat", "message": f"{meta['key']} with {meta['progression']} repeats a bed in "
                      "brief.recent: change the key or the progression"})
    found += bed_findings(r)
    found += check_cues(folder, brief)  # the sound writer's cue sheet (Plan 2d)
    if not found:  # earshot's ears, as the render stage's QA will use them: a deaf bed goes back to the writer
        frac = listen(folder).get("music_frac")
        if not isinstance(frac, (int, float)):
            raise RuntimeError(f"earshot returned no music_frac ({frac!r})")
        if frac < MIN_MUSIC_FRAC:
            found.append({"code": "deaf", "message": f"earshot hears music in {frac} of its 5 s windows, under "
                          f"{MIN_MUSIC_FRAC}: make the bed audible under the voice and between the lines"})
    return found


def _system(ws: dict, out: Path) -> Path:
    out.write_text(GUIDE.read_text(encoding="utf-8") + "\n\n---\n\n"
                   + (Path(ws["dir"]) / ws["playbooks"]["music"]).read_text(encoding="utf-8"), encoding="utf-8")
    return out


def music(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    ws = load(job.workspace)
    folder, work = job.dir / "music", job.dir / "music_work"
    folder.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    brief = build_brief(job)
    (folder / "brief.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")
    fit_narration(job.dir / "narration.wav", brief["duration_s"], folder / "narration.wav",
                  max_hold_s=format_of(ws)["max_hold_s"])
    system = _system(ws, work / "system_music.md")
    narration = json.loads((job.dir / "script.json").read_text(encoding="utf-8"))["narration"]
    prompt = "\n\n".join([
        f"Compose the music bed for this {ws['display_name']} {format_of(ws)['label']}. Follow the harness guide and "
        "the music playbook in your system prompt. Your folder holds brief.json (read it first) and narration.wav. Write score.js, run render "
        "until it is ok, then listen and read listen/sound.png, and refine until the playbook's targets hold.",
        f"Your commands (Bash, from this folder, exactly as written):\n{RENDER_CMD}\n{LISTEN_CMD}",
        SFX_HELP,
        "## Narration", narration])
    calls, cost, rounds, findings = [], 0.0, 0, []
    while True:
        with job.regeneration(f"the music check still fails after the regeneration budget: "
                              f"{sorted({f['code'] for f in findings})}" if findings else None):
            r = call("music", prompt, cwd=folder, tools=TOOLS, allow=ALLOW, system_file=system, claude_bin=claude_bin,
                     usage_log=job.dir / "usage.jsonl", sensor=sensor,
                     transcript=work / f"transcript_{len(calls) + 1}.jsonl")
        cost += r.cost_usd or 0.0
        calls.append({"cost_usd": r.cost_usd, "seconds": r.seconds, "turns": r.turns, "timed_out": r.timed_out})
        if r.isolation:
            raise RuntimeError(f"music run not isolated, output unused: {r.isolation}")
        if not (folder / "score.js").is_file():
            raise RuntimeError(f"the music writer left no score.js (exit {r.exit_code}, timed out {r.timed_out})")
        findings = _check(folder, brief)
        if not findings:
            break
        rounds += 1
        (work / f"findings_{rounds}.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
        prompt = ("The factory's check failed on the score in your folder. Fix every finding with the smallest change "
                  "that clears it, keeping the playbook's direction, then run render and listen before you finish.\n\n"
                  f"Your commands:\n{RENDER_CMD}\n{LISTEN_CMD}\n\n{SFX_HELP}\n\n## Findings\n"
                  + json.dumps(findings, indent=2))
    (work / "calls.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    meta = json.loads((folder / "render.json").read_text(encoding="utf-8"))["meta"]
    return {"runs": len(calls), "fix_rounds": rounds, "cost_usd": round(cost, 4), "meta": meta}
