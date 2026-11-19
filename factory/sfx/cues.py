"""The sound writer's cue sheet, checked before any render (Plan 2d Task 2; FAILURE_MODES.md, sound 9-15). Findings go
back to the writer through the music stage's fix loop. No sheet is not a finding: the render places the palette."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .library import OK_LICENCES, rows

LOUD_DB, AMBIENCE_MAX_DB, NEAR_EVENT_S = -18.0, -24.0, 0.15
MAX_PER_10S, MAX_AT_ONCE, MAX_SAME_ID = 8, 3, 4
SHEET = "sfx.json must be {\"cues\": [{\"id\", \"t\", \"gain_db\", \"serves\", optional fade_in_s, fade_out_s, pitch_st, loop_s}]}"


def load_cues(folder: Path) -> list[dict] | None:
    """The sheet's cues, None when there is no sheet; raises ValueError for a malformed one."""
    f = folder / "sfx.json"
    if not f.exists():
        return None
    cues = json.loads(f.read_text(encoding="utf-8"))["cues"]
    if not isinstance(cues, list):
        raise ValueError(SHEET)
    for c in cues:
        if not (isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("serves", ""), str)
                and all(type(c.get(k, 0)) in (int, float) for k in ("t", "gain_db", "fade_in_s", "fade_out_s",
                                                                       "pitch_st", "loop_s")) and "t" in c):
            raise ValueError(SHEET)
    return cues


def check_cues(folder: Path, brief: dict) -> list[dict]:
    try:
        cues = load_cues(folder)
    except (ValueError, KeyError, TypeError):
        return [{"code": "cue_sheet", "message": SHEET}]
    if not cues:
        return []
    known, found = {r["id"]: r for r in rows()}, []
    dur, gaps = float(brief["duration_s"]), brief.get("gaps", [])
    events, words = [e["t"] for e in brief.get("events", [])], brief.get("words", [])
    spans = []

    def add(code: str, message: str) -> None:
        if code not in {f["code"] for f in found}:
            found.append({"code": code, "message": message})
    for c in cues:
        row, t, gain = known.get(c["id"]), float(c["t"]), float(c.get("gain_db", -12))
        if row is None:
            add("unknown_sound", f"{c['id']} isn't in the library: run sfx get (or sfx forge) for it first")
        elif row.get("license") not in OK_LICENCES:
            add("licence", f"{c['id']} is {row.get('license')}: only public-domain, earshot and code-made sounds")
        if not 0 <= t < dur:
            add("cue_time", f"{c['id']} at {t} s is outside the film's 0-{dur} s")
        length = float(c.get("loop_s") or (row or {}).get("duration_s") or 0.5)
        peak = t + float((row or {}).get("peak_s") or 0)
        if c.get("serves") == "ambience":
            if gain > AMBIENCE_MAX_DB:
                add("ambience_loud", f"ambience {c['id']} at {gain} dB: keep ambience at {AMBIENCE_MAX_DB} dB or lower")
            continue
        spans.append((t, t + length, c["id"]))
        spoken = any(w["start"] <= peak <= w["end"] for w in words)
        if gain > LOUD_DB and spoken and not any(a <= peak <= b for a, b in gaps) \
                and not any(abs(peak - e) <= NEAR_EVENT_S for e in events):
            add("covers_speech", f"{c['id']} peaks at {peak:.2f} s inside a spoken word at {gain} dB: move it into a "
                                 f"gap, onto an event, or under {LOUD_DB} dB")
    if any(sum(1 for b, _, _ in spans if a <= b < a + 10) > MAX_PER_10S for a, _, _ in spans):
        add("too_many", f"more than {MAX_PER_10S} cues in 10 s: keep only the motivated ones")
    if any(sum(1 for b, e, _ in spans if b <= a < e) > MAX_AT_ONCE for a, _, _ in spans):
        add("pileup", f"more than {MAX_AT_ONCE} sounds at once")
    if (top := Counter(i for _, _, i in spans).most_common(1)) and top[0][1] > MAX_SAME_ID:
        add("repeat", f"{top[0][0]} plays {top[0][1]} times: at most {MAX_SAME_ID}, so it stays a sound, not a loop")
    return found
