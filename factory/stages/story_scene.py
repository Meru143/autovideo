"""story_scene: a story's shot table becomes its film (story plan Task 6; FAILURE_MODES.md, story scene). The page is
built from data: the canvas stage, word-by-word captions from timing.json, the shots' signs as enamel plates, and a
data-event marker for every sound the shots name. A writer run stages the shots in film.js on the channel's kit
(engine.js, cast.js, sets.js in ./brand/). Then the scene's check loop (the renderer's check, fix rounds from the
shared budget) gates it, as for any composition, and the render, review and repair stages work on it unchanged."""
from __future__ import annotations

import array
import html
import json
import math
import wave
from pathlib import Path

from ..claude_call import SENSOR, call
from ..jobs import Job
from ..kit import missing
from ..kit_build import FORBIDDEN
from ..library import fetch as library_fetch, index as library_index
from ..workspace import format_of, load
from .scene import CODE_TOOLS, _brand, _check_loop, _guide, _system

CAPTION_TOP, CAPTION_H = 1050, 170  # one word at a time, in a band above 65% of the height (1248 px)
FINAL_SIGN_S = 2.05  # HyperFrames' floor for a film's last line (line_unread: at least 2 s), plus margin

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=1080, height=1920" />
__CSS__
<style>
html, body { margin: 0; padding: 0; background: #1c1e22; }
#root[data-composition-id="main"] { position: relative; width: 1080px; height: 1920px; overflow: hidden; background: #1c1e22; }
#stage { position: absolute; inset: 0; z-index: 0; }
#stage canvas { display: block; width: 1080px; height: 1920px; }
#captions { position: absolute; inset: 0; z-index: 10; pointer-events: none; }
.sign { position: absolute; left: 90px; top: 300px; width: 900px; display: flex; justify-content: center; z-index: 20;
        opacity: 0; visibility: hidden; pointer-events: none; }
.sign span { display: block; box-sizing: border-box; padding: 14px 30px; background: var(--ink); color: var(--bone);
             border: 5px solid var(--bone); border-radius: 10px; font-family: var(--font); font-weight: var(--w-sign);
             line-height: 1.1; letter-spacing: 0.04em; text-transform: uppercase; text-align: center; white-space: nowrap; }
</style>
<script src="./vendor/gsap.min.js"></script>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="__D__" data-width="1080" data-height="1920">
  <div id="stage" data-layout-allow-overflow></div>
  <div id="captions" data-composition-id="captions" data-composition-src="compositions/captions.html" data-start="0"
       data-duration="__D__" data-track-index="1" data-track-kind="captions" data-width="1080" data-height="1920"></div>
__SIGNS__
  <div id="events" style="display: none">
__EVENTS__
  </div>
</div>
<script>
(function () {  // the hf-seek bridge: frame(t) is pure, so seeks may arrive in any order
  window.__hf = window.__hf || {};
  window.__hf.buildReady = window.__hf.buildReady || {};
  var done;
  window.__hf.buildReady["film"] = new Promise(function (r) { done = r; });
  window.__filmDone = done;
  window.__filmT = 0;
  window.addEventListener("hf-seek", function (e) {
    window.__filmT = e.detail.time;
    if (window.__filmRender) window.__filmRender(e.detail.time);
  });
})();
</script>
<script type="module">
import * as E from "./brand/engine.js";
import { frame } from "./film.js";
const cv = document.createElement("canvas");
cv.width = E.W; cv.height = E.H;
document.getElementById("stage").appendChild(cv);
E.init(cv);
window.__filmRender = frame;
try { frame(window.__filmT || 0); } finally { window.__filmDone(); }
</script>
<script>
(function () {
  var tl = gsap.timeline({ paused: true });
__TWEENS__
  window.__timelines = window.__timelines || {};
  window.__timelines["main"] = tl;
})();
</script>
</body>
</html>
"""

CAPTIONS = """<template>
  <style>
    #caps { position: absolute; left: 0; top: 0; width: 1080px; height: 1248px; pointer-events: none; }
    .cap { position: absolute; left: 0; top: __TOP__px; width: 1080px; height: __H__px; display: flex; align-items: center;
           justify-content: center; opacity: 0; visibility: hidden; }
    .plate { display: block; box-sizing: border-box; padding: 16px 38px; background: var(--ink); border-radius: 12px; }
    .w { display: block; font-family: var(--font); font-weight: var(--w-caption); line-height: 1.08; text-align: center;
         white-space: nowrap; color: var(--bone); letter-spacing: 0.02em; }
  </style>
  <div id="root" data-composition-id="captions" data-width="1080" data-height="1920">
    <div id="caps">
__ROWS__
    </div>
  </div>
  <script>
    (function () {
      var tl = gsap.timeline({ paused: true });
      var rows = Array.prototype.slice.call(document.querySelectorAll("#caps .cap"));
      rows.forEach(function (row, i) {
        var w = row.querySelector(".w"), plate = row.querySelector(".plate");
        var at = parseFloat(w.getAttribute("data-at")), end = parseFloat(w.getAttribute("data-word-end"));
        var nx = rows[i + 1] ? parseFloat(rows[i + 1].querySelector(".w").getAttribute("data-at")) : null;
        var out = nx === null ? end + 0.6 : nx - end < 0.3 ? nx : end + 0.25;
        var pop = Math.min(0.12, (out - at) / 2);
        tl.fromTo(row, { autoAlpha: 0 }, { autoAlpha: 1, duration: pop, ease: "power3.out", immediateRender: false }, at);
        tl.fromTo(plate, { scale: 1.06 }, { scale: 1, duration: pop, ease: "power3.out", immediateRender: false }, at);
        tl.set(row, { autoAlpha: 0 }, out);
      });
      window.__timelines = window.__timelines || {};
      window.__timelines["captions"] = tl;
    })();
  </script>
</template>
"""


def envelope(wav: Path, fps: int = 30) -> list[float]:
    """The voice's loudness per film frame, 0-1: the speaking character's mouth follows it."""
    with wave.open(str(wav)) as w:
        sr, ch, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    if width != 2:
        raise RuntimeError(f"{wav.name}: 16-bit PCM expected, got {8 * width}-bit")
    s = array.array("h", frames)[::ch]
    step = max(1, sr // fps)
    rms = [math.sqrt(sum(x * x for x in s[i:i + step]) / max(1, len(s[i:i + step]))) for i in range(0, len(s), step)]
    top = sorted(rms)[int(len(rms) * 0.98)] or 1.0
    return [round(min(1.0, r / top), 2) for r in rms]


def _page(project: Path, table: list[dict], timing: dict, duration: float) -> None:
    """index.html and compositions/captions.html, from data alone (rebuilt before each check)."""
    signs, tweens, events = [], [], []
    # the film's last sign is its closing line, which HyperFrames holds to 2 s: it stays up FINAL_SIGN_S from its start,
    # past its own shot if need be, and no sign follows it (FAILURE_MODES story scene, closing line)
    last = max((k for k, s in enumerate(table) if s.get("signs")), default=None)
    for k, s in enumerate(table):
        for e in s.get("events", []):
            events.append(f'    <i data-event="{html.escape(str(e))}" data-at="{s["start"]:.3f}"></i>')
        if s.get("signs"):
            text = str(s["signs"][0]).strip()
            size = min(58, int(1500 / max(1, len(text))))
            signs.append(f'  <div class="sign" id="sign{k}"><span data-text-box style="font-size: {size}px">'
                         f'{html.escape(text)}</span></div>')
            tweens.append(f'  tl.set("#sign{k}", {{ autoAlpha: 1 }}, {s["start"]:.3f});'
                          f'\n  tl.set("#sign{k}", {{ autoAlpha: 0 }}, '
                          f'{max(s["end"], min(duration, s["start"] + FINAL_SIGN_S)) if k == last else s["end"]:.3f});')
    d = f"{duration:.3f}"
    # every stylesheet the kit holds (its tokens and fonts), by glob: no brand file is named in code
    css = "\n".join(f'<link rel="stylesheet" href="./brand/{p.name}" />' for p in sorted((project / "brand").glob("*.css")))
    page = PAGE.replace("__CSS__", css).replace("__D__", d).replace("__SIGNS__", "\n".join(signs))
    (project / "index.html").write_text(page.replace("__EVENTS__", "\n".join(events)).replace("__TWEENS__", "\n".join(tweens)),
                                        encoding="utf-8")
    rows = []
    for i, w in enumerate(timing["words"]):
        word = html.escape(w["w"].strip().upper())
        size = min(116, int(820 / max(1, 0.86 * len(word))))
        rows.append(f'      <div class="cap"><div class="plate"><div class="w" id="w{i}" data-text-box '
                    f'data-at="{w["start"]:.3f}" data-word-end="{w["end"]:.3f}" style="font-size: {size}px">{word}'
                    '</div></div></div>')
    (project / "compositions").mkdir(exist_ok=True)
    (project / "compositions" / "captions.html").write_text(
        CAPTIONS.replace("__TOP__", str(CAPTION_TOP)).replace("__H__", str(CAPTION_H)).replace("__ROWS__", "\n".join(rows)),
        encoding="utf-8")


def _data(job: Job, project: Path, ep: dict, table: list[dict], timing: dict, duration: float) -> None:
    """story_data.js: what film.js stages, as plain data."""
    beats, words, k = [], timing["words"], 0
    for b in ep["beats"]:  # each beat's span on the timed words, by word count (story_check's mapping)
        n = len(b["text"].split())
        a, z = min(k, len(words) - 1), min(k + n, len(words)) - 1
        beats.append({"role": b["role"], "text": b["text"], "start": words[a]["start"], "end": words[max(a, z)]["end"]})
        k += n
    data = {"DURATION_S": round(duration, 3), "SHOTS": table, "BEATS": beats,
            "WORDS": [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in words],
            "VOICE": envelope(job.dir / "narration.wav")}
    (project / "story_data.js").write_text("// built from the job's data by the story_scene stage; read only\n" + "\n".join(
        f"export const {k} = {json.dumps(v)};" for k, v in data.items()) + "\n", encoding="utf-8")


def story_scene(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    ws = load(job.workspace)
    project, work, kit = job.dir / "project", job.dir / "scene", Path(ws["dir"]) / "brand"
    ep = json.loads((job.dir / "episode.json").read_text(encoding="utf-8"))
    timing = json.loads((job.dir / "timing.json").read_text(encoding="utf-8"))
    table = json.loads((job.dir / "shot_table.json").read_text(encoding="utf-8"))
    # a table from before engine Task 5 is a bare list with no assets: it was staged on the kit, checked then
    assets, table = (table.get("assets") or [], table["shots"]) if isinstance(table, dict) else ([], table)
    inventory = kit / "inventory.json"
    if not inventory.exists() or not all((kit / m).exists() for m in ("engine.js", "cast.js", "sets.js")):
        raise RuntimeError("the kit isn't built: brand needs inventory.json, engine.js, cast.js and sets.js")
    if gaps := missing(assets, json.loads(inventory.read_text(encoding="utf-8")), library_index(ws)):
        raise RuntimeError(f"the shot table marks these library, and neither the library nor the kit has them: {gaps}")
    project.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    _brand(ws, project)
    library_fetch(project, ws, assets)  # a library asset sits with the reel's own, where its imports resolve
    duration = max(s["end"] for s in table)
    _page(project, table, timing, duration)
    _data(job, project, ep, table, timing, duration)
    (project / "shot_plan.json").write_text(json.dumps(table, indent=2), encoding="utf-8")
    film, marker = project / "film.js", work / "writer.done.json"
    if marker.exists() and film.exists():  # a stop after the writer: resume at the checks, never pay for it twice
        return _check_loop(job, None, claude_bin=claude_bin, sensor=sensor)
    prompt = "\n\n".join([
        f"Write film.js in your current folder: this {ws['display_name']} episode's film, staged shot by shot on the "
        "kit in ./brand/ (engine.js, cast.js, sets.js), as the scene playbook in your system prompt describes. "
        "story_data.js holds the shots, the beats with their times, the timed words and the voice's loudness per frame. "
        "index.html and the captions are built from data: leave them as they are. Read ./brand/cast.js and "
        "./brand/sets.js for the names, marks and cameras you can use.",
        "Assets (the list below; the owner's rule: each reel builds its own, the library only for the same thing): "
        "build each `new` asset as its own file, assets/<kind>/<name>.js in your folder, from the kit's building "
        "blocks (engine.js's solids, cast.js's character spec shape and POSES, sets.js's lb, sign and shop), as "
        "`export default {kind, name, description, build}`, its `description` copied from the shot table's below word "
        "for word (the library saves an asset only when the two match); import it in film.js and call "
        "E.define(a.kind, a.name, a.build), so cast.js and sets.js find it by name. A `library` asset from the "
        "library is already drawn and copied in: import it from ./assets/<kind>/<name>.js and E.define it the same "
        "way, or, for the base kit's own pieces, use its name in cast.js or sets.js. Build each character once and use the same one in every shot "
        "it appears in. A new asset film.js never defines fails the check before the render.",
        "## Assets (the shot table's)", json.dumps(assets, indent=1),
        "## The story", ep["narration"],
        "## Shots (story_data.js SHOTS)", json.dumps(table, indent=1)])
    r = call("scene", prompt, cwd=project, tools=CODE_TOOLS, add_dirs=(_guide(job),),
             system_file=_system(ws, "scene", work / "system_scene.md"), claude_bin=claude_bin,
             usage_log=job.dir / "usage.jsonl", sensor=sensor)
    if not r.isolation and film.exists():
        marker.write_text(json.dumps({"seconds": r.seconds, "cost_usd": r.cost_usd, "exit": r.exit_code,
                                      "timed_out": r.timed_out}), encoding="utf-8")
    for _ in range(2):  # the static scan: a film that keeps state or reaches out goes back before the renderer
        bad = sorted({m.group(0) for m in FORBIDDEN.finditer(film.read_text(encoding="utf-8"))}) if film.exists() else []
        if not bad:
            break
        with job.regeneration(f"film.js still uses {bad} after the regeneration budget"):
            r = call("fix", f"film.js in your folder uses {bad}. It must be a pure function of t: replace them, keeping "
                            "the staging.", cwd=project, tools=CODE_TOOLS, add_dirs=(_guide(job),),
                     system_file=_system(ws, "fix", work / "system_fix.md"), claude_bin=claude_bin,
                     usage_log=job.dir / "usage.jsonl", sensor=sensor)
    _page(project, table, timing, duration)  # the page as the data says, whatever a writer did to it
    return _check_loop(job, r, claude_bin=claude_bin, sensor=sensor)
