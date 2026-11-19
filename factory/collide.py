"""Separate drawings colliding (FAILURE_MODES.md, collide): HyperFrames' layout check covers text, never drawing
against drawing. Serves the project on localhost, seeks its one timeline in headless Chrome (collide.mjs) every
quarter second plus each declared event, and returns the [data-solid] pairs that overlap and stay overlapped."""
from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path

from .audio import declared_events, root_duration
from .procs import run_cli
from .render import BAKEOFF, CheckFailed, _Quiet, install_vendor

STEP_S = 0.25
HELD_S = 0.25  # a run must span at least one full step (2+ samples): a crossing mid-move isn't a collision
TIMEOUT_S = 300


class Unchecked(RuntimeError):
    """The composition can't be checked as written (page errors, not one timeline): the scene writer can fix it."""


def _times(index: Path) -> list[float]:
    try:
        duration = root_duration(index)
    except ValueError as e:
        raise Unchecked(str(e)) from None
    grid = [round(i * STEP_S, 3) for i in range(int(duration / STEP_S))]
    return sorted({*grid, *(round(t, 3) for _, t in declared_events(index) if 0 <= t < duration)})


def _held(pairs: list[dict], times: list[float]) -> list[dict]:
    """Runs of consecutive flagged samples per pair, kept when they span HELD_S."""
    hits: dict[tuple[str, str], dict[float, float]] = {}
    for p in pairs:
        hits.setdefault(tuple(sorted((p["a"], p["b"]))), {})[p["t"]] = p["share"]
    found = []
    for (a, b), at in hits.items():
        run: list[float] = []
        for t in [*times, None]:  # None closes the last run
            if t is not None and t in at:
                run.append(t)
                continue
            if run and run[-1] - run[0] >= HELD_S:
                found.append({"from_s": run[0], "to_s": run[-1], "a": a, "b": b, "share": max(at[x] for x in run)})
            run = []
    return sorted(found, key=lambda f: f["from_s"])


def check(project: Path) -> list[dict]:
    """[{from_s, to_s, a, b, share}] for held collisions; raises RuntimeError when the page can't be checked."""
    install_vendor(project)
    times = _times(project / "index.html")
    paths = json.loads((BAKEOFF / "paths.json").read_text(encoding="utf-8"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(project)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = run_cli([paths["hf_node"], str(Path(__file__).with_name("collide.mjs")), str(BAKEOFF),
                     paths["chrome_headless_shell"], f"http://127.0.0.1:{server.server_port}/index.html",
                     json.dumps(times)], timeout_s=TIMEOUT_S)
    finally:
        server.shutdown()
        server.server_close()
    if r.timed_out:  # its tree (node and Chrome) is dead
        raise Unchecked(f"the collision check didn't finish in {TIMEOUT_S} s")
    if r.returncode != 0:
        raise RuntimeError(f"collision check couldn't load the page: {r.stderr.strip()[-600:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out["errors"]:
        raise Unchecked(f"page errors during the collision check: {out['errors'][:3]}")
    if out.get("fatal"):  # FAILURE_MODES collide 10: the page's own code threw while the gate seeked it
        raise Unchecked(f"the page's script threw while the collision check seeked its timeline: {out['fatal'][:300]}")
    if out.get("bad"):  # collide 9
        raise Unchecked("; ".join(f"{b['el']} has data-connects=\"{b['value']}\", which isn't a list of ids"
                                  for b in out["bad"][:3]) + ". Write the ids it meets, like data-connects=\"#a #b\"")
    if out["timelines"] != ["main"]:
        raise Unchecked(f"expected one timeline, window.__timelines.main; found {out['timelines']}. Keep the Short in "
                        "index.html with one timeline, window.__timelines[\"main\"], and no scene files")
    return _held(out["pairs"], times)


def gate(project: Path) -> None:
    """The scene loop's collision gate: CheckFailed with one finding per held collision, or for a page the check
    can't read, so the fix run gets them like any renderer finding. A page with nothing marked data-solid fails too:
    an unmeasured page never passes."""
    if "data-solid" not in (project / "index.html").read_text(encoding="utf-8"):
        raise CheckFailed([{"code": "no_solids", "message": "No drawing is marked data-solid, so the collision check "
                            "can't measure anything. Mark each separate drawing (a character, a gauge, a card, a tower, "
                            "a line) data-solid; a deliberate overlap data-layout-allow-overlap, and a line meeting "
                            "its node data-connects=\"#node\"."}], {})
    try:
        hits = check(project)
    except Unchecked as e:
        raise CheckFailed([{"code": "collide_unchecked", "message": f"{e}."}], {}) from None
    if hits:
        raise CheckFailed([{"code": "drawing_collision", "selector": f"{h['a']}, {h['b']}", "time": h["from_s"],
                            "message": f"{h['from_s']}-{h['to_s']} s: {h['a']} and {h['b']} overlap ({h['share']:.0%} "
                            "of the smaller box). Keep about 24 px of ground between them, or declare "
                            "data-connects when they meet on purpose."} for h in hits], {})
