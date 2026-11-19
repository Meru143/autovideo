"""kit_build: a story channel's cast and sets, built once for the stories it tells and grown per story (story plan
Tasks 4-5; FAILURE_MODES.md, kit: the cast build, the set build). A writer run (code tools, the test key while
building) writes cast.js or sets.js in its own build folder, beside a copy of engine.js, the inventory and a sheet
harness. The kit check measures it, and its findings go back for up to 3 fix rounds. Only a module that passes is
installed into brand/, and its stills are made for the operator's look approval."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import OUT, ROOT
from .claude_call import SENSOR, call
from .workspace import blocked

CODE_TOOLS = ("Read", "Write", "Edit")
FORBIDDEN = re.compile(r"\bDate\b|Math\.random|performance\.|setTimeout|setInterval|requestAnimationFrame"
                       r"|\bfetch\s*\(|XMLHttpRequest|\bimport\s*\(|https?://")
RATIO, FACETS, ROUNDS = (5.5, 6.2), (14, 26), 3

_HEAD = """<!doctype html>
<html><head><meta charset="utf-8" /><style>html,body{margin:0;background:#1C1E22}
#root{position:relative;width:1080px;height:1920px;overflow:hidden}</style>
<script src="./vendor/gsap.min.js"></script></head>
<body><div id="root" data-composition-id="main" data-start="0" data-duration="__DURATION__" data-width="1080"
data-height="1920"><div id="stage"></div></div>
<script>(function () { window.__hf = window.__hf || {}; window.__hf.buildReady = window.__hf.buildReady || {};
var done; window.__hf.buildReady["sheet"] = new Promise(function (r) { done = r; }); window.__sheetDone = done;
window.__sheetT = 0; window.addEventListener("hf-seek", function (e) { window.__sheetT = e.detail.time;
if (window.__sheetRender) window.__sheetRender(e.detail.time); });
window.__timelines = window.__timelines || {}; window.__timelines["main"] = gsap.timeline({ paused: true }); })();</script>
<script type="module">
import * as E from "./engine.js";
import * as K from "./__MODULE__";
const inv = JSON.parse(document.getElementById("inv").textContent);
const cv = document.createElement("canvas");
cv.width = E.W; cv.height = E.H; cv.style.width = "1080px"; cv.style.height = "1920px";
document.getElementById("stage").appendChild(cv);
E.init(cv);
"""
_TAIL = """window.__sheetRender = render;
try { render(window.__sheetT || 0); } finally { window.__sheetDone(); }
</script>
<script type="application/json" id="inv">__INVENTORY__</script>
</body></html>
"""
# each character for 3 s: front, three-quarter, side; each location for 1 s from its default camera
KINDS = {
    "cast": {"module": "cast.js", "per": 3, "names": "characters", "render": """const ids = Object.keys(inv.characters);
const first = (kind, id) => Object.entries(inv[kind] || {}).filter(([, e]) => (e.characters || []).includes(id)).map(([n]) => n)[0];
function render(t) {
  const id = ids[Math.min(ids.length - 1, Math.floor(t / 3))], view = Math.floor(t % 3);
  E.setEnv(E.makeEnv("dusk"));
  E.setCam([0, 1.15, 5.2], [0, 0.95, 0], 30);
  E.drawSky(null);
  E.gRect(-8, -8, 8, 8, E.PALETTE.slate, 4);
  K.drawCharacter(id, { at: [0, 0, 0], facing: [0, -0.75, -1.57][view], pose: first("poses", id) || K.POSES[0],
                        poseT: t % 3, expression: first("expressions", id) || K.EXPRESSIONS[0], talk: 0, props: [], t });
  E.flush();
  E.finish(t);
}
"""},
    "sets": {"module": "sets.js", "per": 1, "names": "locations", "render": """const ids = Object.keys(inv.locations);
function render(t) {
  const id = ids[Math.min(ids.length - 1, Math.floor(t))], L = K.LOCATIONS[id];
  const time = (inv.locations[id].times || ["dusk"])[0];
  E.setEnv(E.makeEnv(time));
  E.setCam(L.camera.pos, L.camera.target, L.camera.fov || 40);
  E.drawSky([-0.6, 0.12, 60]);
  K.drawSet(id, { t, time });
  E.flush();
  E.finish(t);
}
"""},
}


def findings(build: Path, kind: str, ws: dict | None = None) -> tuple[list[str], dict]:
    """The kit check on build/<kind>.js: a static scan, then kit_check.mjs's measurements, by name. Sets also check
    their sign words against the channel's blocked words and canon list."""
    mod = build / KINDS[kind]["module"]
    if not mod.exists():
        return [f"no {mod.name} was written"], {}
    src = mod.read_text(encoding="utf-8", errors="replace")
    fails = sorted({f"not allowed in {mod.name}: {m.group(0)!r}" for m in FORBIDDEN.finditer(src)})
    _flat_engine(build)
    r = subprocess.run(["node", str(ROOT / "factory" / "kit_check.mjs"), str(build), kind], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return fails + [f"the {kind} check crashed: {r.stderr.strip()[-400:]}"], {}
    fails += data["errors"] + [f"missing {m}" for m in data["missing"]]
    for cid, c in data.get("characters", {}).items():
        if not RATIO[0] <= c["ratio"] <= RATIO[1]:
            fails.append(f"{cid}: {c['ratio']} heads tall; the cast is 5.5-6 heads tall (toy avatars run 4-4.5)")
        if not FACETS[0] <= c["headFacets"] <= FACETS[1]:
            fails.append(f"{cid}: the head has {c['headFacets']} facets; a cut-gem head has 14-20, plus its face plate "
                         "and nose")
    if kind == "sets" and ws is not None:
        words = ws.get("blocked_words", []) + ws.get("canon_blocklist", [])
        fails += [f"a sign says {w!r}: every sign, product and logo is invented" for w in
                  blocked(" | ".join(data.get("signs", [])), words)]
    return fails, data


def _flat_engine(build: Path) -> None:
    """A build folder holds engine.js beside the module, where a project keeps it in brand/: its three.js import
    points at the folder's own vendor copy instead of the project's (../vendor)."""
    from .render import install_vendor
    eng = build / "engine.js"
    if eng.exists():
        eng.write_bytes(eng.read_bytes().replace(b'"../vendor/three.module.js"', b'"./vendor/three.module.js"'))
    install_vendor(build)


def _harness(build: Path, kind: str, inventory: dict) -> None:
    from .render import install_vendor
    k = KINDS[kind]
    n = max(1, len(inventory.get(k["names"], {})))
    page = (_HEAD + k["render"] + _TAIL).replace("__MODULE__", k["module"]).replace("__DURATION__", str(k["per"] * n))
    (build / "sheet.html").write_text(page.replace("__INVENTORY__", json.dumps(inventory).replace("</", "<\\/")),
                                      encoding="utf-8")
    install_vendor(build)


def build(ws: dict, kind: str, *, claude_bin: list[str] | None = None, sensor: Path | None = SENSOR, root: Path = OUT,
          stills: bool = True) -> dict:
    """Write, check and install brand/<kind>.js for brand/inventory.json; an existing module is the starting
    point (growth). Returns {rounds, findings per round, measure, build, cost_usd, stills}; raises RuntimeError after
    ROUNDS failed fixes, leaving the installed module untouched."""
    k, kit = KINDS[kind], Path(ws["dir"]) / "brand"
    inv_file = kit / "inventory.json"
    if not inv_file.exists():
        raise RuntimeError("no brand/inventory.json: run `factory kit inventory` and get the operator's OK first")
    inventory = json.loads(inv_file.read_text(encoding="utf-8"))
    out_dir = root / "kit_builds" / ws["name"] / kind
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    shutil.copyfile(kit / "engine.js", out_dir / "engine.js")
    shutil.copyfile(inv_file, out_dir / "inventory.json")
    grow = (kit / k["module"]).exists()
    if grow:
        shutil.copyfile(kit / k["module"], out_dir / k["module"])
    _harness(out_dir, kind, inventory)
    what = ("characters, poses, expressions and props" if kind == "cast" else "locations, at their times of day")
    ask = (f"Build {k['module']} in your folder, in passes as your system prompt's 'Work in passes' says: save a small "
           f"first version within your first few steps, then add one piece at a time with Edit, about 250 lines "
           f"at most per write, until it covers exactly the {what} in inventory.json, on engine.js. "
           f"sheet.html shows your work; read it before you stop."
           + (f" {k['module']} is the current one: extend it for the inventory, and keep everything it already draws."
              if grow else ""))
    system = Path(ws["dir"]) / ws["playbooks"][kind]
    rounds, cost = [], 0.0
    for n in range(ROUNDS + 1):
        r = call(kind, ask, cwd=out_dir, tools=CODE_TOOLS, system_file=system, claude_bin=claude_bin, sensor=sensor,
                 usage_log=out_dir / "usage.jsonl")
        cost += r.cost_usd or 0.0
        if r.isolation:
            raise RuntimeError(f"{kind} run not isolated, output unused: {r.isolation}")
        fails, data = findings(out_dir, kind, ws)
        rounds.append(fails)
        if not fails:
            break
        if n == ROUNDS:
            raise RuntimeError(f"{k['module']} still fails after {ROUNDS} fix rounds: {fails[:6]}")
        ask = f"Fix {k['module']} in your folder; the {kind} check found these:\n" + "\n".join(f"- {f}" for f in fails)
    looks = None
    if all((kit / m).is_file() or m == k["module"] for m in ("cast.js", "sets.js")):  # the looks the shots writer
        from .kit import add_descriptions, describe                                    # matches against
        looks = describe(kit, module=out_dir / k["module"])  # read first: a kit that doesn't load installs nothing
    shutil.copyfile(out_dir / k["module"], kit / k["module"])
    if looks is not None:
        add_descriptions(kit, looks)
    result = {"rounds": len(rounds), "findings": rounds, "measure": data.get("characters", {}), "build": str(out_dir),
              "cost_usd": round(cost, 4)}
    if stills:
        result["stills"] = kit_stills(out_dir, kind, inventory)
    return result


def kit_stills(out_dir: Path, kind: str, inventory: dict) -> list[str]:
    """The look-approval stills, from the sheet (HyperFrames snapshot): per character one PNG of front, three-quarter
    and side side by side; per location one PNG from its default camera."""
    from .render import _hf
    k = KINDS[kind]
    ids, per = list(inventory.get(k["names"], {})), k["per"]
    shots = out_dir / "stills"
    shutil.rmtree(shots, ignore_errors=True)
    shutil.copyfile(out_dir / "sheet.html", out_dir / "index.html")  # snapshot opens a project's index.html
    times = [per * i + v + 0.5 for i in range(len(ids)) for v in range(per)]
    r = subprocess.run([*_hf(), "snapshot", str(out_dir), "--at", ",".join(f"{x:g}" for x in times), "--no-end", "-o",
                        str(shots), "--keep"], cwd=out_dir, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"snapshot failed: {r.stderr.strip()[-400:]}")
    out = []
    for i, name in enumerate(ids):
        # snapshot names a frame at-<t>s.png or frame-NN-at-<t>s.png, by its version (0.9.9 writes the second)
        frames = [next(iter(sorted(shots.glob(f"*at-{per * i + v + 0.5:g}s.png"))), None) for v in range(per)]
        if not all(frames):
            continue
        sheet = shots / f"{name}.png"
        if per == 1:
            shutil.copyfile(frames[0], sheet)
        else:
            subprocess.run(["ffmpeg", "-y", "-v", "error", *[a for f in frames for a in ("-i", str(f))], "-filter_complex",
                            f"hstack=inputs={per},scale=1080:-1", str(sheet)], check=True, timeout=120)
        out.append(str(sheet))
    return out
