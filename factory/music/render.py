"""The music harness (Plan 2c Task 2; design 8.5 item 12; FAILURE_MODES.md, music): renders a writer's score.js to
bed.wav in the pinned headless Chrome with Tone.js, seeded and sandboxed, and records its provenance."""
from __future__ import annotations

import hashlib
import http.server
import json
import math
import re
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from ..gpu import gpu_lock
from ..procs import run_cli
from ..render import BAKEOFF
from .instruments import verify

HERE = Path(__file__).parent
ENGINE = "Tone.js 15.1.22"
TYPES = {".html": "text/html", ".js": "text/javascript", ".flac": "audio/flac"}
KEY = re.compile(r"^[A-G](#|b)?$")
SILENT_DBFS, SILENT_MIN_S, WIN_S = -60.0, 0.5, 0.05


class RenderFailed(RuntimeError):
    """The score can't be rendered as written; the message goes back to the writer."""


def _server(files: dict[str, Path], misses: list[str]) -> http.server.ThreadingHTTPServer:
    class Allowlist(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = files.get(self.path.split("?", 1)[0])
            if path is None:
                if not self.path.endswith(".map"):  # a source map is only fetched by devtools, never run
                    misses.append(self.path)
                return self.send_error(404)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Allowlist)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _meta_problems(meta) -> list[str]:
    if not isinstance(meta, dict):
        return ["score.js exports no meta object"]
    out = [f"meta.{k} is missing" for k in ("key", "mode", "bpm", "progression", "mood") if k not in meta]
    if "key" in meta and not KEY.match(str(meta["key"])):
        out.append(f"meta.key {meta['key']!r} isn't a note name such as D, F# or Bb")
    if "bpm" in meta and not (isinstance(meta["bpm"], (int, float)) and 40 <= meta["bpm"] <= 200):
        out.append(f"meta.bpm {meta['bpm']!r} isn't a tempo between 40 and 200")
    if "progression" in meta and not (isinstance(meta["progression"], list) and meta["progression"]):
        out.append("meta.progression must list the chords, such as ['i', 'IV', 'VII', 'i']")
    return out


def levels(wav: Path) -> dict:
    """Length, peak and RMS in dBFS, silent spans (50 ms windows under -60 dBFS) of 0.5 s or more, and the mono fold-down
    loss: a phone speaker plays (L+R)/2, so sides that cancel go quiet (identical sides lose 0 dB, unrelated ones ~3)."""
    with wave.open(str(wav)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, ch).astype(np.float64) / 32768.0
    db = lambda v: round(20 * math.log10(max(float(v), 1e-9)), 2)  # noqa: E731
    win = int(sr * WIN_S)
    n = len(x) // win
    rms = np.sqrt((x[: n * win].mean(axis=1).reshape(n, win) ** 2).mean(axis=1))
    quiet = [bool(q) for q in 20 * np.log10(np.maximum(rms, 1e-9)) < SILENT_DBFS] + [False]
    spans, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * WIN_S >= SILENT_MIN_S:
                spans.append([round(start * WIN_S, 2), round(i * WIN_S, 2)])
            start = None
    left, right = x[:, 0], x[:, -1]
    sides, mono = ((left ** 2).mean() + (right ** 2).mean()) / 2, (((left + right) / 2) ** 2).mean()
    return {"seconds": round(len(x) / sr, 3), "peak_dbfs": db(np.abs(x).max()), "rms_dbfs": db(np.sqrt((x ** 2).mean())),
            "silent_spans": spans, "mono_loss_db": round(10 * math.log10(max(sides, 1e-12) / max(mono, 1e-12)), 2)}


def render_bed(folder: Path, *, duration_s: float | None = None) -> dict:
    """Render folder/score.js into folder/bed.wav, write render.json and provenance.json, and return the report.
    RenderFailed carries what the writer must fix: its own error, a request outside the harness, or a bad meta."""
    if not (folder / "score.js").is_file():
        raise RenderFailed("no score.js in the music folder")
    found = verify()  # InstrumentMissing propagates: a broken install isn't the writer's to fix
    brief = json.loads((folder / "brief.json").read_text(encoding="utf-8"))
    duration, seed = float(duration_s if duration_s is not None else brief["duration_s"]), int(brief["seed"])
    samples = {name: p for name, p in found.items() if name != "tone"}
    files = {"/harness.html": HERE / "harness.html", "/vendor/Tone.js": found["tone"],
             **{f"/samples/{p.name}": p for p in samples.values()},
             **{f"/{p.name}": p for p in folder.glob("*.js")}}  # the score and its own modules
    paths = json.loads((BAKEOFF / "paths.json").read_text(encoding="utf-8"))
    misses: list[str] = []
    server = _server(files, misses)
    try:
        with tempfile.TemporaryDirectory() as td:
            inp, wav = Path(td) / "input.json", Path(td) / "bed.wav"
            inp.write_text(json.dumps({"duration": duration, "seed": seed, "brief": brief,
                                       "samples": {n: p.name for n, p in samples.items()}}), encoding="utf-8")
            with gpu_lock():  # one heavy GPU job at a time, across the videos running side by side (9 Nov)
                r = run_cli([paths["hf_node"], str(HERE / "render.mjs"), str(BAKEOFF), paths["chrome_headless_shell"],
                             f"http://127.0.0.1:{server.server_port}/harness.html", str(inp), str(wav)], timeout_s=600)
            if r.timed_out:  # node and its Chrome are dead; a score that never finishes is the writer's to fix
                raise RenderFailed("score.js didn't finish rendering in 600 s: look for a loop or a promise that "
                                   "never settles")
            if r.returncode != 0:
                raise RuntimeError(f"the music harness didn't run: {r.stderr.strip()[-600:]}")
            out = json.loads(r.stdout.strip().splitlines()[-1])
            if outside := out["blocked"] + misses:
                raise RenderFailed(f"score.js reached outside the harness: {outside[:5]}. Use only Tone and piano().")
            if out["error"] or out["errors"]:
                raise RenderFailed(f"score.js failed: {(out['error'] or out['errors'][0])[:1500]}")
            if problems := _meta_problems(out["meta"]):
                raise RenderFailed("; ".join(problems))
            (folder / "bed.wav").write_bytes(wav.read_bytes())
    finally:
        server.shutdown()
        server.server_close()
    report = {**levels(folder / "bed.wav"), "duration_s": duration, "meta": out["meta"]}
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    palette = brief.get("palette") or {}
    prov = {"engine": ENGINE, "chrome": out["chrome"], "seed": seed, "duration_s": duration,
            "score_sha256": sha(folder / "score.js"), "brief_sha256": sha(folder / "brief.json"),
            "palette": palette.get("palette"), "palette_version": palette.get("version"),
            "instruments": {n: sha(p) for n, p in found.items()}, "bed_sha256": sha(folder / "bed.wav")}
    (folder / "render.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (folder / "provenance.json").write_text(json.dumps(prov, indent=2), encoding="utf-8")
    return report
