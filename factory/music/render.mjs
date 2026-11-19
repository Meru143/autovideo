// Runs the music harness in the pinned headless Chrome (Plan 2c Task 2; FAILURE_MODES.md, music). Run by render.py:
//   node render.mjs <bakeoff-dir> <chrome> <harness-url> <input-json> <out-wav>
// input-json: {duration, seed, brief, samples}. Prints one JSON line: {meta, error, errors, blocked, chrome}.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const [bakeoff, chrome, url, inputPath, outWav] = process.argv.slice(2);
const load = createRequire(path.join(bakeoff, "package.json"));  // puppeteer-core from the bake-off's install
const puppeteer = load("puppeteer-core");
const input = JSON.parse(fs.readFileSync(inputPath, "utf-8"));
const origin = new URL(url).origin;
const errors = [], blocked = [];
// Every connection that isn't loopback (the harness server) hits a dead proxy, workers and service workers included,
// and WebRTC may only use proxied UDP: the paths page.setRequestInterception can't see (Plan 2c final review I4).
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: [
  "--disable-gpu", "--proxy-server=http://127.0.0.1:9", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"] });
try {
  const page = await browser.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (r) => {  // only this server; it serves only the allowlist (render.py)
    if (r.url().startsWith(origin + "/")) return r.continue();
    blocked.push(r.url());
    return r.abort();
  });
  await page.evaluateOnNewDocument((seed) => {  // mulberry32, before Tone or the score can draw a number
    let a = seed >>> 0;
    Math.random = () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }, input.seed);
  await page.evaluateOnNewDocument(() => {  // and a score that reaches for them gets an error naming the API
    const off = (obj, name) => Object.defineProperty(obj, name, { configurable: false, get() {
      throw new Error(`${name} is disabled in the music harness: use only Tone and piano()`); } });
    for (const name of ["WebSocket", "WebTransport", "RTCPeerConnection", "webkitRTCPeerConnection", "EventSource"]) off(window, name);
    off(navigator, "serviceWorker");
  });
  await page.goto(url, { waitUntil: "load", timeout: 60000 });
  await page.waitForFunction(() => window.harnessReady === true, { timeout: 30000 });
  const r = await page.evaluate(
    (inp) => window.renderScore(inp).catch((e) => ({ error: String((e && e.stack) || e) })),
    { duration: input.duration, brief: input.brief, samples: input.samples },
  );
  if (r.wav) fs.writeFileSync(outWav, Buffer.from(r.wav, "base64"));
  console.log(JSON.stringify({ meta: r.meta ?? null, error: r.error ?? null, errors, blocked,
                               chrome: await browser.version() }));
} finally {
  await browser.close();
}
