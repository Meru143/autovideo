// The check pass (scene engine Tasks 3-4): geometry only, no browser and no canvas. Prints one JSON line.
//   node factory/checkpass.mjs <project> [--fps 10] [--duration s] [--shots shot_table.json] [--out report.json]
//     steps <project>/film.js's frame(t) at fps over the film (index.html's data-duration) and collects
//     E.checkFrame()'s clips, and with a shot table: a character a shot names on screen (areaShare >= 0.002) in under
//     half its frames (missing), and a "new" asset that film.js never E.define-d (undefined):
//     {frames, hits: [{t, owner, part, other, depthM}], missing: [{shot, owner, seen_share}], undefined: [{kind, name}], wall_s}
//   node factory/checkpass.mjs <project> --kit [--out report.json]
//     defines <project>/assets/<kind>/<name>.js, then builds every character (the kit's and the defined ones, by name
//     and as an inline spec) in every pose, holding the defined props, and every location, one at a time:
//     Then every character sits and stands on every location's marks, against that set. A location's seat marks are
//     data (LOCATIONS[name].seats): a stander there is staged too, and its clip goes to seatStand (it must be one: the
//     film-mode check refuses a stander on a seat mark the same way), not to hits. errors: a listed seat mark where the
//     sitter rests on no seat, or an unlisted mark where it does.
//     {built, hits, face, props, seats, seatMarks, seatStand, sink, errors, wall_s}. sink: how deep each sitter on a
//     seat mark is in the seat. face: a hair or hat shell over its own face plate.
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2), proj = resolve(args[0]);
const opt = (k, d) => { const i = args.indexOf(k); return i > 0 && i + 1 < args.length ? args[i + 1] : d; };
const url = (...p) => pathToFileURL(join(proj, ...p)).href;
const E = await import(url("brand", "engine.js"));
E.init(null, { check: true });
const t0 = performance.now();
const report = args.includes("--kit") ? await kit() : await film();
report.wall_s = +((performance.now() - t0) / 1000).toFixed(2);
const text = JSON.stringify(report);
if (opt("--out")) writeFileSync(opt("--out"), text);
console.log(text);

async function film() {
  const F = await import(url("film.js"));
  const dur = +(opt("--duration") ?? (readFileSync(join(proj, "index.html"), "utf8").match(/data-duration="([\d.]+)"/) || [])[1]);
  if (!(dur > 0)) throw new Error("checkpass: no film duration (index.html's data-duration, or --duration)");
  const table = opt("--shots") && existsSync(opt("--shots")) ? JSON.parse(readFileSync(opt("--shots"), "utf8")) : {};
  const shots = Array.isArray(table) ? table : table.shots || [], seen = shots.map(() => ({})), frames = shots.map(() => 0);
  const fps = +opt("--fps", 10), n = Math.max(1, Math.round(dur * fps)), hits = [];
  for (let i = 0; i < n; i++) {
    const t = +(i / fps).toFixed(4);
    F.frame(t);
    const c = E.checkFrame();
    for (const h of c.hits) hits.push({ t, ...h });
    shots.forEach((s, k) => {
      if (t < s.start || t >= s.end) return;
      frames[k]++;
      for (const ch of s.characters || []) if ((c.onScreen[ch.id]?.areaShare || 0) >= 0.002) seen[k][ch.id] = (seen[k][ch.id] || 0) + 1;
    });
  }
  const missing = [];
  shots.forEach((s, k) => {
    for (const ch of s.characters || []) {
      const share = frames[k] ? (seen[k][ch.id] || 0) / frames[k] : 1;
      if (share < 0.5) missing.push({ shot: k, owner: ch.id, seen_share: +share.toFixed(2) });
    }
  });
  const reg = E.registry();  // ruling E1: no assets list, no undefined check
  const undefined_ = (Array.isArray(table) ? [] : table.assets || []).filter((a) => a.source === "new" && !Object.prototype.hasOwnProperty.call(reg[a.kind] || {}, a.name))
    .map((a) => ({ kind: a.kind, name: a.name }));
  return { frames: n, hits, missing, undefined: undefined_ };
}

async function kit() {
  const C = await import(url("brand", "cast.js")), St = await import(url("brand", "sets.js"));
  const dir = join(proj, "assets");
  if (existsSync(dir)) for (const kind of readdirSync(dir)) for (const f of readdirSync(join(dir, kind))) {
    if (!f.endsWith(".js")) continue;
    const a = (await import(url("assets", kind, f))).default;
    E.define(a.kind, a.name, a.build);
  }
  const reg = E.registry(), out = { built: [], hits: [], face: [], props: new Set(), seats: {}, seatMarks: [], seatStand: [], sink: [], errors: [] };
  const look = (what) => {  // one piece's pushes: shells over their own face plate, clips, then clear
    const L = E.S.solids, faces = L.filter((s) => s.face);
    for (const s of L) {
      if (s.part === "prop") out.props.add(`${s.owner}/${s.part}`);
      if (s.shell) for (const f of faces) if (f.owner === s.owner && E.solidDepth(s, f) > 0.0005) out.face.push({ what, owner: s.owner, part: s.part });
    }
    for (const h of E.checkFrame().hits) out.hits.push({ what, ...h });
    E.flush();
    out.built.push(what);
  };
  const piece = (what, draw) => {
    try { draw(); look(what); } catch (e) { out.errors.push(`${what}: ${e.message}`); E.checkFrame(); E.flush(); }
  };
  E.setEnv(E.makeEnv("dusk"));
  E.setCam([0, 1.1, 7], [0, 0.95, 0], 28);
  const held = Object.keys(reg.prop);
  for (const id of [...Object.keys(C.CHARACTERS), ...Object.keys(reg.character)])
    for (const pose of [...C.POSES, ...Object.keys(reg.pose)])
      for (const poseT of [0, 0.25, 0.5, 0.75, 1.9]) {
        piece(`${id} ${pose} ${poseT}`, () => C.drawCharacter(id, { pose, poseT, t: poseT, talk: 0.5, props: held }));
      }
  for (const [name, spec] of Object.entries(reg.character)) piece(`${name} (inline spec)`, () => C.drawCharacter({ ...spec, id: name }, { pose: "walk", props: held }));
  const ids = [...Object.keys(C.CHARACTERS), ...Object.keys(reg.character)];
  for (const name of [...Object.keys(St.LOCATIONS), ...Object.keys(reg.set)]) {
    const L = St.LOCATIONS[name] || reg.set[name];
    E.setCam(L.camera.pos, L.camera.target, L.camera.fov);
    E.setEnv(E.makeEnv((L.times || ["dusk"])[0]));
    let set = [];
    piece(name, () => {
      St.drawSet(name, { t: 1 });
      set = E.S.solids.slice();
      out.seats[name] = set.filter((s) => s.part === "seat").length;
    });
    // every character sitting and standing on every mark, against the set's solids (drawn once). L.seats lists the
    // seat marks (ruling E8: a mark's pose intent is data); a stander on one must clip, and goes to seatStand
    const seats = set.filter((s) => s.part === "seat"), listed = new Set(L.seats || []);
    const up = (p) => [p[0], p[1] + 0.01, p[2]];
    const raised = seats.map((q) => ({ ...q, v: q.v.map(up), lo: up(q.lo), hi: up(q.hi) }));
    for (const mark of listed) if (!(L.marks || {})[mark]) out.errors.push(`${name}: seat mark "${mark}" is not among its marks`);
    for (const [mark, m] of Object.entries(L.marks || {})) for (const id of ids) {
      const at = `${name}.${mark}`, seatMark = listed.has(mark);
      if (seatMark) out.seatMarks.push(`${id} at ${at}`);
      for (const pose of ["sit", "stand"]) {
        E.S.solids = set.slice();
        const before = out.hits.length;
        piece(`${id} ${pose} at ${at}`, () => {
          C.drawCharacter(id, { at: [m[0], m[1] || 0, m[2]], facing: m[3] || 0, pose, poseT: 1, t: 1 });
          if (pose !== "sit") return;
          // resting on a seat: touching one, or within 1 cm above it (a gap that small doesn't show)
          const seated = E.S.solids.some((s) => s.owner === id && raised.some((q) => E.solidDepth(s, q) > 0));
          if (seatMark) out.sink.push({ what: `${id} at ${at}`, depthM: +Math.max(0, ...E.S.solids.filter((s) => s.owner === id).flatMap((s) => seats.map((q) => E.solidDepth(s, q)))).toFixed(3) });
          if (seated !== seatMark) out.errors.push(`${id} sitting at ${at} ${seated ? "rests on a seat, but the mark isn't in seats" : "rests on no seat, but the mark is in seats"}`);
        });
        if (pose === "stand" && seatMark) out.seatStand.push(...out.hits.splice(before));
      }
    }
  }
  out.props = [...out.props].sort();
  return out;
}
