// Separate drawings colliding in a HyperFrames composition (FAILURE_MODES.md, collide). Run by factory/collide.py:
//   node collide.mjs <bakeoff-dir> <chrome> <url> <times-json>
// Seeks window.__timelines.main to each time, applies HyperFrames' clip windows (data-start/data-duration), measures
// every visible [data-solid] element, and prints one JSON line: {pairs: [{t, a, b, share}], timelines, errors}.
import { createRequire } from "node:module";
import path from "node:path";

const [bakeoff, chrome, url, timesJson] = process.argv.slice(2);
const load = createRequire(path.join(bakeoff, "package.json"));  // puppeteer-core from the bake-off's install
const puppeteer = load("puppeteer-core");
const times = JSON.parse(timesJson);
const errors = [];
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ["--disable-gpu"] });
try {
  const page = await browser.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewport({ width: 1080, height: 1920 });
  await page.evaluateOnNewDocument(() => { window.__timelines = {}; });  // the HyperFrames runtime's registry
  await page.goto(url, { waitUntil: "load", timeout: 60000 });
  await page.waitForFunction(() => Object.keys(window.__timelines).length > 0, { timeout: 30000 });
  await page.evaluate(() => document.fonts.ready);
  const result = await page.evaluate((times) => { try {
    const keys = Object.keys(window.__timelines);
    // data-connects lists ids by commas or spaces (FAILURE_MODES collide 8); a value that is no selector connects
    // nothing and is reported by element, never thrown (collide 9)
    const bad = [], connects = new Map();
    for (const el of document.querySelectorAll("[data-connects]")) {
      connects.set(el, el.dataset.connects.split(/[\s,]+/).filter((s) => {
        if (!s) return false;
        try { document.querySelector(s); return true; } catch { bad.push({ el: el.id ? "#" + el.id : el.tagName.toLowerCase(), value: el.dataset.connects }); return false; }
      }));
    }
    const tl = window.__timelines.main || window.__timelines[keys[0]];
    const clips = [...document.querySelectorAll("[data-start][data-duration]")];
    const name = (el) => (el.id ? "#" + el.id : el.tagName.toLowerCase() + "." + [...el.classList].join("."));
    const pairs = [];
    for (const t of times) {
      tl.seek(t, false);
      for (const el of clips) {  // HyperFrames shows a clip only inside its window
        const start = parseFloat(el.dataset.start), dur = parseFloat(el.dataset.duration);
        el.style.visibility = t >= start && t < start + dur ? "" : "hidden";
      }
      const solids = [...document.querySelectorAll("[data-solid]")]
        .filter((el) => el.checkVisibility({ opacityProperty: true, visibilityProperty: true }))
        .map((el) => ({ el, r: el.getBoundingClientRect() }))
        .filter((s) => s.r.width > 0 && s.r.height > 0);
      for (let i = 0; i < solids.length; i++) {
        for (let j = i + 1; j < solids.length; j++) {
          const a = solids[i], b = solids[j];
          if (a.el.contains(b.el) || b.el.contains(a.el)) continue;  // a badge on its figure
          // a deliberate overlap is marked on both drawings: one mark alone would silence every collision of a moving
          // drawing (FAILURE_MODES collide 11)
          if (a.el.closest("[data-layout-allow-overlap]") && b.el.closest("[data-layout-allow-overlap]")) continue;
          const linked = (x, y) => (connects.get(x.el) || []).some((s) => y.el.matches(s));
          if (linked(a, b) || linked(b, a)) continue;  // a line meeting its node on purpose
          const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
          const h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
          if (w <= 4 || h <= 4) continue;
          const share = (w * h) / Math.min(a.r.width * a.r.height, b.r.width * b.r.height);
          if (share > 0.1) pairs.push({ t, a: name(a.el), b: name(b.el), share: Math.round(share * 100) / 100 });
        }
      }
    }
    return { pairs, timelines: keys, bad };
  } catch (e) {  // page code throwing mid-seek is the page's error, for the fix run (collide 10)
    return { pairs: [], timelines: Object.keys(window.__timelines || {}), bad: [], fatal: String((e && e.message) || e) };
  } }, times);
  console.log(JSON.stringify({ ...result, errors }));
} finally {
  await browser.close();
}
