// The cast check's measurements (story plan Task 4; FAILURE_MODES.md, kit). Run by factory/kit_build.py:
//   node factory/kit_check.mjs <build_dir>
// Loads <build_dir>/engine.js and cast.js against a recording canvas, draws every character of inventory.json in
// each of its poses, expressions and props, twice at fixed times, and prints one JSON line: errors, missing names,
// determinism, and per character the head's facet count and height ratio (from the parts the cast tags).
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const dir = resolve(process.argv[2]);
const inv = JSON.parse(readFileSync(join(dir, "inventory.json"), "utf8"));
const out = { errors: [], missing: [], deterministic: true, characters: {} };

// the engine runs in check mode (no DOM in Node): a drawing's record is its recorded solids and the 2D calls
const record = (r) => { const s = E.S.solids.map((x) => [x.v, x.part]); E.checkFrame(); return JSON.stringify([r.calls, s]); };
function recorder() {
  const calls = [];
  const target = {};
  const ctx = new Proxy(target, {
    get: (_, k) => (k === "canvas" ? { width: 1080, height: 1920 } : (...a) => {
      calls.push([String(k), ...a.map((x) => (typeof x === "number" ? Math.round(x * 1000) / 1000 : typeof x))]);
      return k === "createRadialGradient" || k === "createLinearGradient" ? { addColorStop() {} } : undefined;
    }),
    set: (_, k, v) => { calls.push(["=", String(k), typeof v === "number" ? Math.round(v * 1000) / 1000 : String(v)]); return true; },
  });
  return { calls, canvas: { getContext: () => ctx } };
}

const E = await import(pathToFileURL(join(dir, "engine.js")).href);
const mode = process.argv[3] || "cast";
let C;
try {
  C = await import(pathToFileURL(join(dir, `${mode}.js`)).href);
} catch (e) {
  out.errors.push(`${mode}.js does not load: ${e.message}`);
  console.log(JSON.stringify(out));
  process.exit(0);
}
if (mode === "sets") {  // story plan Task 5: every inventory location, at each of its times of day, twice
  out.signs = Array.isArray(C.SIGNS) ? C.SIGNS.map(String) : [];
  if (!Array.isArray(C.SIGNS)) out.errors.push("sets.js exports no SIGNS list (every word it paints)");
  for (const [name, e] of Object.entries(inv.locations || {})) {
    const L = (C.LOCATIONS || {})[name];
    if (!L) { out.missing.push(`location: ${name}`); continue; }
    if (!String(L.desc || "").trim()) out.missing.push(`description: location ${name}`);  // the shots writer's "same one"
    if (Object.keys(L.marks || {}).length < 2) out.errors.push(`${name}: fewer than 2 marks for characters to stand on`);
    if (!L.camera || !L.camera.pos || !L.camera.target) out.errors.push(`${name}: no default camera {pos, target, fov}`);
    for (const time of e.times && e.times.length ? e.times : ["dusk"]) {
      const once = () => {
        const r = recorder();
        E.init(null, { check: true });
        E.setEnv(E.makeEnv(time));
        const cam = L.camera || { pos: [0, 1.6, 8], target: [0, 1.2, 0], fov: 40 };
        E.setCam(cam.pos, cam.target, cam.fov || 40);
        C.drawSet(name, { t: 3.3, time });
        E.flush();
        return record(r);
      };
      try {
        if (once() !== once()) { out.deterministic = false; out.errors.push(`${name} at ${time} draws differently twice`); }
      } catch (err) {
        out.errors.push(`${name} at ${time}: ${err.message}`);
      }
    }
  }
  console.log(JSON.stringify(out));
  process.exit(0);
}
for (const [kind, list] of [["character", Object.keys(C.CHARACTERS || {})], ["pose", C.POSES || []],
                            ["expression", C.EXPRESSIONS || []], ["prop", C.PROPS || []]]) {
  for (const name of Object.keys(inv[kind + "s"] || {})) if (!list.includes(name)) out.missing.push(`${kind}: ${name}`);
}
// every piece carries its one-line look: the shots writer judges "the same one" by it (engine Task 5 review p6)
for (const id of Object.keys(inv.characters || {}))
  if ((C.CHARACTERS || {})[id] && !String(C.CHARACTERS[id].desc || "").trim()) out.missing.push(`description: character ${id}`);
for (const p of Object.keys(inv.props || {}))
  if ((C.PROPS || []).includes(p) && !String((C.PROP_DESC || {})[p] || "").trim()) out.missing.push(`description: prop ${p}`);
const uses = (kind, id) => Object.entries(inv[kind] || {}).filter(([, e]) => (e.characters || []).includes(id)).map(([n]) => n);

function draw(id, args) {
  const r = recorder();
  E.init(null, { check: true });
  E.setEnv(E.makeEnv("dusk"));
  E.setCam([0, 1.1, 7], [0, 0.95, 0], 28);
  C.drawCharacter(id, args);
  E.flush();
  return record(r);
}
for (const id of Object.keys(inv.characters || {})) {
  const poses = uses("poses", id), exprs = uses("expressions", id), props = uses("props", id);
  const base = { at: [0, 0, 0], facing: 0, pose: poses[0] || (C.POSES || [])[0], poseT: 0, expression: exprs[0] || (C.EXPRESSIONS || [])[0], talk: 0, props: [], t: 0 };
  try {
    E.resetStats();
    draw(id, base);
    const h = E.STATS.parts.head, b = E.STATS.parts.body;
    if (!h || !b) {
      out.errors.push(`${id}: no polygons tagged {part: "head"} and {part: "body"}`);
      continue;
    }
    const total = Math.max(h.y1, b.y1) - Math.min(h.y0, b.y0), head = h.y1 - h.y0;
    out.characters[id] = { headFacets: h.n, heightM: +total.toFixed(3), headM: +head.toFixed(3), ratio: +(total / head).toFixed(2) };
    for (const pose of poses) for (const poseT of [0, 0.7, 1.9]) for (const expression of exprs.length ? exprs : [base.expression]) {
      const args = { ...base, pose, poseT, expression, talk: (poseT * 0.37) % 1, props, t: 2.3 + poseT, facing: 0.6 };
      if (draw(id, args) !== draw(id, args)) {
        out.deterministic = false;
        out.errors.push(`${id}: ${pose} at ${poseT} s draws differently twice`);
      }
    }
  } catch (e) {
    out.errors.push(`${id}: ${e.message}`);
  }
}
console.log(JSON.stringify(out));
