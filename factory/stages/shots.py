"""shots: the timed episode becomes a shot table naming the reel's own assets (the library's only for an exact
match), checked by a validator before anything is built or rendered (story plan Task 2; design 5.4.2;
FAILURE_MODES.md, story 10-14; engine Task 5)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..claude_call import SENSOR, call
from ..jobs import Job
from ..kit import KIT_KIND, missing as kit_gaps
from ..library import index as library_index
from ..workspace import format_of, load, story_rules_of
from .story_check import beat_times

NUM, STR = {"type": "number"}, {"type": "string"}
ASSETS = {"type": "array", "items": {"type": "object", "required": ["name", "kind", "source"],  # engine Task 5
                                    "properties": {"name": STR, "kind": {"enum": list(KIT_KIND)},
                                                   "source": {"enum": ["new", "library"]}, "description": STR}}}
SCHEMA = {"type": "object", "required": ["assets", "shots"], "properties": {"assets": ASSETS, "shots": {"type": "array", "items": {
    "type": "object", "required": ["start", "end", "location", "time", "characters", "camera", "framing", "events", "signs",
                                   "threat_visible", "enforcer_visible", "note"],
    "properties": {"start": NUM, "end": NUM, "location": STR, "time": {"enum": ["dusk", "night", "day"]},
                   "characters": {"type": "array", "items": {"type": "object", "required": ["id", "pose", "expression", "props"],
                                                             "properties": {"id": STR, "pose": STR, "expression": STR,
                                                                            "props": {"type": "array", "items": STR}}}},
                   "camera": STR, "framing": STR, "events": {"type": "array", "items": STR},
                   "signs": {"type": "array", "items": STR}, "threat_visible": {"type": "boolean"},
                   "enforcer_visible": {"type": "boolean"}, "note": STR}}}}}
EPS = 0.05  # seconds of slack on every edge


def _picture(s: dict) -> tuple:
    return s["location"], s["framing"], json.dumps(s["characters"], sort_keys=True)


# A sign is read on the page (story_scene): HyperFrames' check holds each line to line_unread's floor and type to 28 px
# (text_below_floor), and the page sets a sign at min(58, 1500 / characters) px, so 40 characters keep it over 37 px.
SIGN_MAX_CHARS = 40


def sign_floor_s(text: str) -> float:
    """HyperFrames' reading floor for a line (line_unread: 0.40 s up to 2 words, else 0.75 x (0.5 + 0.3 a word)),
    plus 0.05 s of margin; a word has a letter or digit, so a dash between words doesn't count."""
    n = sum(1 for w in text.split() if any(c.isalnum() for c in w))
    return round((0.40 if n <= 2 else 0.75 * (0.5 + 0.3 * n)) + 0.05, 2)


def kit_inventory(ws: dict) -> dict | None:
    """The channel's base kit inventory (brand/inventory.json, with each character's, set's and prop's description),
    or None with no kit. A shot table may mark one of its pieces "library" only when it is the same thing; with no kit,
    every asset is the reel's own ("new")."""
    f = Path(ws["dir"]) / "brand" / "inventory.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def validate_shots(table: list[dict], ep: dict, timing: dict, ws: dict, assets: list[dict] = ()) -> list[str]:
    """The shot table's failures, by name and time; assets: the table's assets list (engine Task 5)."""
    if not table:
        return ["no shots"]
    sr, hold, shots = story_rules_of(ws), format_of(ws)["max_hold_s"], sorted(table, key=lambda s: s["start"])
    (lo, hi), voice_end, film_max, failures = sr["shot_s"], timing["duration_s"], timing["audio_duration_s"] + hold, []
    if shots[0]["start"] > EPS:
        failures.append(f"gap at the start: the first shot begins at {shots[0]['start']:.2f} s")
    for a, b in zip(shots, shots[1:]):
        if b["start"] - a["end"] > EPS:
            failures.append(f"gap from {a['end']:.2f} to {b['start']:.2f} s")
        elif a["end"] - b["start"] > EPS:
            failures.append(f"overlap: a shot starts at {b['start']:.2f} s before the last ends at {a['end']:.2f} s")
    end = shots[-1]["end"]
    if end < voice_end - EPS:
        failures.append(f"the shots end at {end:.2f} s, before the voice at {voice_end:.2f} s")
    elif end > film_max + EPS:
        failures.append(f"the shots run to {end:.2f} s, past the voice plus the {hold} s hold ({film_max:.2f} s)")
    for k, s in enumerate(shots):
        if not lo - EPS <= s["end"] - s["start"] <= hi + EPS:
            failures.append(f"shot {k} at {s['start']:.2f} s is {s['end'] - s['start']:.2f} s, outside {lo}-{hi} s")
    span = None  # (start, end, picture) of consecutive static-camera shots of one picture
    for s in shots:
        joins = span and s["camera"] == "static" and span[2] == _picture(s) and abs(s["start"] - span[1]) <= EPS
        span = (span[0], s["end"], span[2]) if joins else ((s["start"], s["end"], _picture(s)) if s["camera"] == "static"
                                                           else None)
        if span and span[1] - span[0] > sr["max_static_s"] + EPS:
            failures.append(f"static from {span[0]:.2f} to {span[1]:.2f} s, over {sr['max_static_s']} s")
            span = None
    for k, s in enumerate(shots):  # the page shows each sign for its shot alone, and the film writer can't change it
        for sign in s.get("signs") or []:
            text, need = str(sign).strip(), sign_floor_s(str(sign))
            if len(text) > SIGN_MAX_CHARS:
                failures.append(f"shot {k}'s sign {text!r} is {len(text)} characters; keep a sign to {SIGN_MAX_CHARS}")
            if s["end"] - s["start"] < need:
                failures.append(f"shot {k} at {s['start']:.2f} s holds its sign {text!r} {s['end'] - s['start']:.2f} s, "
                                f"under the {need:.2f} s it takes to read: shorten the sign or lengthen the shot")
    failures += asset_failures(shots, assets, ws)
    if not any(s["enforcer_visible"] and s["start"] <= sr["enforcer_by_s"] for s in shots):
        failures.append(f"no enforcer on screen by {sr['enforcer_by_s']} s")
    times, roles = beat_times(ep, timing), [b["role"] for b in ep["beats"]]
    for s in shots:
        if not s["threat_visible"]:
            continue
        if s["start"] < sr["threat_after"] * end:
            failures.append(f"threat visible at {s['start']:.2f} s ({s['start'] / end:.0%}), before "
                            f"{sr['threat_after']:.0%} of runtime")
        mid = (s["start"] + s["end"]) / 2
        role = roles[max((i for i, (a, _) in enumerate(times) if a <= mid), default=0)]
        if role not in ("threat", "flip", "callback"):
            failures.append(f"the shot at {s['start']:.2f} s shows the threat during a {role} beat the narration keeps "
                            "unseen")
    return failures


def _snake(text: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def asset_failures(shots: list[dict], assets: list[dict], ws: dict) -> list[str]:
    """The owner's rule (10 Nov): each reel builds its own assets, and takes a library one only when the library holds
    that very thing. A "library" name must be in the library or the base kit; a "new" one needs a description; every
    name a shot uses must be in assets (FAILURE_MODES story 18: a story asked for pieces nobody built)."""
    # snake_case, plurals kept: kit.name() would trim "umbrellas" to the kit's "umbrella", the near match the rule refuses
    failures = [f"an asset name is written as its snake_case id: {a.get('name')!r} must be {_snake(a.get('name', ''))!r}"
                for a in assets if a.get("name") != _snake(a.get("name", ""))]
    failures += [f"library {g}: the library serves only the same thing, by its exact name, so build it new, with a "
                 "description" for g in kit_gaps(assets, kit_inventory(ws) or {}, library_index(ws))]
    failures += [f"the new {a['kind']} {a['name']} has no description: describe what this reel builds"
                 for a in assets if a.get("source") == "new" and not str(a.get("description") or "").strip()]
    listed = {(a.get("kind"), a.get("name")) for a in assets}
    used = {("set", s["location"]) for s in shots} | {
        x for s in shots for c in s["characters"]
        for x in [("character", c["id"]), ("pose", c["pose"]), *(("prop", p) for p in c["props"])]}
    failures += [f"the shots use the {kind} {n}, which isn't in assets: list it, new with a description, or library "
                 "if the library holds the same thing (names exactly as listed)" for kind, n in sorted(used)
                 if (kind, n) not in listed]
    return failures


def _prompt(ws: dict, ep: dict, timing: dict, fb: list[str]) -> str:
    sr, hold = story_rules_of(ws), format_of(ws)["max_hold_s"]
    rows = [f"- {a:.2f}-{b:.2f} s, {beat['role']}: {beat['text']}" for (a, b), beat in zip(beat_times(ep, timing), ep["beats"])]
    rules = [f"- Cover 0 to at least {timing['duration_s']:.2f} s (the voice) and at most "
             f"{timing['audio_duration_s'] + hold:.2f} s (the voice plus the closing hold), no gaps or overlaps.",
             f"- Each shot {sr['shot_s'][0]}-{sr['shot_s'][1]} s; nothing static for over {sr['max_static_s']} s.",
             f"- The enforcer on screen by {sr['enforcer_by_s']} s; the threat visible only from {sr['threat_after']:.0%} "
             "of runtime and only in threat, flip or callback beats.",
             f"- A sign is at most {SIGN_MAX_CHARS} characters, on a shot long enough to read it: 0.45 s for 1-2 words, "
             f"{sign_floor_s('a b c')} s for 3, {sign_floor_s('a b c d')} s for 4, {sign_floor_s('a b c d e')} s for 5 "
             f"(0.75 x (0.5 + 0.3 a word), plus 0.05)."]
    return "\n\n".join(["Write the shot table for this timed episode, as your system prompt describes.",
                        f"## Rule\n{ep['rule']}", "## Beats, timed", "\n".join(rows), "## Must hold", "\n".join(rules),
                        *_kit_section(ws),
                        *(["## Fix these from the last table", "\n".join(f"- {f}" for f in fb)] if fb else [])])


def _kit_section(ws: dict) -> list[str]:
    """The library for the prompt: its items with their descriptions, and the base kit's pieces by name."""
    inv = kit_inventory(ws) or {}
    lines = [f"- {i['kind']} {i['name']}: {i.get('description', '')}" for i in library_index(ws)]
    lines += [f"- {kind} {n}: {e['desc']}" for kind, k in KIT_KIND.items() if kind != "pose"
              for n, e in sorted(inv.get(k, {}).items()) if e.get("desc")]  # an undescribed piece can't be the same
    lines += [f"- poses (building blocks, by name): {', '.join(sorted(inv['poses']))}"] if inv.get("poses") else []
    return ["## The library (use an item only when it is the same thing)", "\n".join(lines or ["- (empty)"]),
            *([f"Expressions the faces can show: {', '.join(sorted(inv['expressions']))}"] if inv.get("expressions") else [])]


def shots(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    """One shot table, retried from the shared budget until the validator passes; writes shot_table.json."""
    ws = load(job.workspace)
    ep = json.loads((job.dir / "episode.json").read_text(encoding="utf-8"))
    timing = json.loads((job.dir / "timing.json").read_text(encoding="utf-8"))
    work, checks = job.dir / "shots", job.dir / "checks"
    work.mkdir(exist_ok=True)
    checks.mkdir(exist_ok=True)
    attempts, fb, cost = [], [], 0.0
    while True:
        with job.regeneration(f"shot checks still failing after the regeneration budget: {fb[:3]}" if fb else None):
            r = call("shots", _prompt(ws, ep, timing, fb), cwd=work, schema=SCHEMA,
                     system_file=Path(ws["dir"]) / ws["playbooks"]["shots"], claude_bin=claude_bin,
                     usage_log=job.dir / "usage.jsonl", sensor=sensor)
        cost += r.cost_usd or 0.0
        if r.isolation:
            raise RuntimeError(f"shots run not isolated, output unused: {r.isolation}")
        if r.data is None:
            raise RuntimeError(f"shots returned no valid structured output (exit {r.exit_code}, timed out {r.timed_out})")
        failures = validate_shots(r.data.get("shots") or [], ep, timing, ws, r.data.get("assets") or [])
        attempts.append({"failures": failures, "cost_usd": r.cost_usd})
        (checks / "shots.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
        if not failures:
            break
        fb = failures
    # with its assets: the check pass reads them (ruling E1), the scene writer builds the new ones
    (job.dir / "shot_table.json").write_text(json.dumps({"assets": r.data["assets"], "shots": r.data["shots"]}, indent=2),
                                             encoding="utf-8")
    return {"attempts": len(attempts), "shots": len(r.data["shots"]), "cost_usd": round(cost, 4)}
