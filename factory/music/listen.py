"""The music writer's ears (Plan 2c Task 3; design 8.5 item 12; FAILURE_MODES.md, music): the bed mixed under the
narration by the factory's own mixer, then earshot's listening sheet and critique, summarised for the writer."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..audio import EARSHOT_PY, SWELL_DB, mix_master
from ..evidence import sound
from ..sfx import library
from ..sfx.cues import load_cues
from .render import render_bed

CUED_ROLES = ("hook", "rehook", "payoff", "signoff", "close")
METRICS = {"music_frac": "music_frac", "tempo": "tempo", "lufs": "lufs_i", "first_sound_s": "first_sound_s",
           "max_gap_s": "max_gap_s", "side_to_mid_db": "side_to_mid_db", "correlation": "correlation"}


def listen(folder: Path) -> dict:
    """Render if any .js is newer than bed.wav, mix the bed under folder/narration.wav, and return earshot's numbers
    plus speech-to-bed; listen/sound.png is the sheet the writer reads."""
    bed = folder / "bed.wav"
    if not bed.exists() or max(p.stat().st_mtime for p in folder.glob("*.js")) > bed.stat().st_mtime:
        render_bed(folder)
    if not Path(os.environ.get("FACTORY_EARSHOT_PY", EARSHOT_PY)).is_file():
        raise RuntimeError("earshot isn't installed (FACTORY_EARSHOT_PY); listen can't hear anything")
    brief = json.loads((folder / "brief.json").read_text(encoding="utf-8"))
    out = folder / "listen"
    out.mkdir(exist_ok=True)
    spans = [(s["start"], s["end"]) for s in brief.get("sentences", [])]
    try:  # the writer's cues, mixed as the render will mix them; a bad sheet or id is the check's finding, not here
        sheet = load_cues(folder) or []
    except (ValueError, KeyError, TypeError):
        sheet = []
    cues, skipped = [], []
    for c in sheet:
        try:
            cues.append({**c, "path": library.path(c["id"]), "start": float(c["t"])})
        except library.SoundRefused:
            skipped.append(c["id"])
    mixed = mix_master(folder / "narration.wav", bed, cues, out / "mix.wav", speech_spans=spans,
                       swell_db=(brief.get("palette") or {}).get("swell_db", SWELL_DB))
    mix = out / "mix.wav"
    cues = sorted({s["start"] for s in brief.get("sentences", []) if s.get("role") in CUED_ROLES}
                  | {e["t"] for e in brief.get("events", [])})
    sound(mix, out, cues)
    critique = json.loads((out / "critique.json").read_text(encoding="utf-8"))
    metrics = {m["metric"]: m.get("value") for m in critique.get("metrics", [])}
    return {**{k: metrics.get(m) for k, m in METRICS.items()},
            "speech_to_bed_lu": mixed["speech_to_bed_lu"], "bed_gain_db": mixed["bed_gain_db"],
            "sfx_count": len(cues), "sfx_skipped": skipped,
            "loudest_cue_over_speech_lu": mixed["loudest_cue_over_speech_lu"],
            "issues": [a["action"] for a in critique.get("actions", [])][:6],
            "verdict": critique.get("verdict"), "sheet": "listen/sound.png"}
