"""The check pass before the render (scene engine Task 4): factory/checkpass.mjs steps an engine film's frame(t) with
no browser and no canvas, and reports clips, characters a shot names but frames off-screen, and new assets the film
never defines. A project is an engine film when it has film.js and its engine has E.checkFrame; any other project has
nothing to check (status)."""
from __future__ import annotations

import json
import re
from itertools import groupby
from pathlib import Path

from . import ROOT
from .render import _hf, _run, install_vendor

RUNNER = ROOT / "factory" / "checkpass.mjs"


# checkFrame among the engine's exports, however it is written (a function, a const, an export list)
EXPORTS_CHECK = re.compile(r"export\s+(?:async\s+)?(?:function\s*\*?|const|let|var)\s+checkFrame\b|export\s*\{[^}]*\bcheckFrame\b")


def status(project: Path) -> str | None:
    """None: not an engine film (no film.js), nothing to check. "run": check it. Otherwise why it is skipped."""
    if not (project / "film.js").is_file():
        return None
    engine = project / "brand" / "engine.js"
    if not engine.is_file():
        return "skipped: no brand/engine.js"
    return "run" if EXPORTS_CHECK.search(engine.read_text(encoding="utf-8")) else "skipped: engine predates checkFrame"


def run(project: Path, *, fps: int = 10, timeout_s: int = 600) -> dict:
    """{frames, hits, missing, undefined, wall_s}, also written to the job's checks/checkpass.json. The shot table is
    the job's shot_table.json beside the project. A runner that crashes, times out or prints no report raises."""
    install_vendor(project)  # the engine imports three.js from ../vendor
    out = project.parent / "checks" / "checkpass.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    r = _run([_hf()[0], str(RUNNER), str(project), "--fps", str(fps), "--shots", str(project.parent / "shot_table.json"),
              "--out", str(out)], cwd=project, timeout_s=timeout_s)
    if r.timed_out:
        raise RuntimeError(f"the check pass timed out after {timeout_s} s on {project}")
    if r.returncode != 0 or not out.is_file():  # node's own error line first, then the stack's tail
        lines = (r.stderr or r.stdout or "no output").strip().splitlines()
        why = next((x for x in lines if x.lstrip().split(":")[0].endswith("Error")), lines[-1])
        raise RuntimeError(f"the check pass failed (exit {r.returncode}): {why.strip()}\n" + "\n".join(lines[-10:]))
    return json.loads(out.read_text(encoding="utf-8"))


def findings(report: dict) -> list[dict]:
    """The report as the render loop's findings, {code, message} (a clip adds start, end and depth_cm: its deepest span): one clip per owner, part and other over a run of
    frames (from-to s, deepest depth), one missing per shot and character, one undefined per asset."""
    out = []
    key = lambda h: (h["owner"], h["part"], h["other"])  # noqa: E731
    for (owner, part, other), hs in groupby(sorted(report["hits"], key=lambda h: (key(h), h["t"])), key=key):
        hs = list(hs)
        spans, a, b = [], hs[0]["t"], hs[0]["t"]
        for h in hs[1:]:  # consecutive sampled frames (0.2 s apart at most at 10 fps) make one span
            if h["t"] - b > 0.21:
                spans.append((a, b))
                a = h["t"]
            b = h["t"]
        spans.append((a, b))
        where = ", ".join(f"{x:.1f}-{y:.1f} s" if y > x else f"{x:.1f} s" for x, y in spans[:6]) + (" ..." if len(spans) > 6 else "")
        into = ("a seat it stands in: a seat mark is for sitting (pose sit), or stand on another mark" if other == "seat"
                else f"{other}'s solids" if other != "set" else "the set")
        deep = max(hs, key=lambda h: h["depthM"])
        a, b = next((x, y) for x, y in spans if x <= deep["t"] <= y)  # the span of the deepest frame: the review's frame
        out.append({"code": "clip", "message": f"{owner}'s {part or 'body'} goes up to {deep['depthM'] * 100:.0f} cm "
                                               f"into {into} at {where}", "start": a, "end": b, "depth_cm": round(deep["depthM"] * 100)})
    for m in report.get("missing", []):
        out.append({"code": "missing", "message": f"shot {m['shot']} names {m['owner']}, who is on screen in only "
                                                  f"{m['seen_share']:.0%} of its frames by the check's count (an insert drawn without "
                                                  f"E.S.owner counts as off screen): look for them in the pictures"})
    for u in report.get("undefined", []):
        out.append({"code": "undefined", "message": f"the shot table's new {u['kind']} {u['name']!r} is never "
                                                    f"E.define-d by film.js: write assets/{u['kind']}/{u['name']}.js and define it"})
    return out
