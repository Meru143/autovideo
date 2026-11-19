"""Sounds the writer makes in code, and its ears for a description (Plan 2d Task 2; FAILURE_MODES.md, sound 16): earshot
(MIT, in the operator's GitHub stars) forges a recipe toward a text target and scores a sound against a description.
A forged pick is a code-made manifest row: no credit, no download."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..audio import EARSHOT_PY, seconds
from .library import SoundRefused, _append, clean_title, lib_dir, path

FORGE_BUDGET, FORGE_KEEP, FORGE_TIMEOUT_S = 48, 3, 1800


def _earshot(args: list[str], timeout_s: int) -> dict:
    py = Path(os.environ.get("FACTORY_EARSHOT_PY", EARSHOT_PY))
    r = subprocess.run([str(py), "-I", "-m", "earshot", *args, "--json"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout_s)
    if r.returncode:
        raise SoundRefused("earshot", (r.stderr or r.stdout)[-300:])
    return json.loads(r.stdout[r.stdout.index("{"):])


def forge(recipe: str, target: str) -> dict:
    """earshot forge <recipe> aimed at target; each pick becomes a code-made row. Returns sanitised names only."""
    if not re.fullmatch(r"[a-z_]{2,32}", recipe):
        raise SoundRefused("unknown_recipe", f"{recipe!r} isn't a recipe name (earshot forge --list)")
    target = clean_title(target) or recipe
    out = lib_dir() / "forge" / f"{recipe}-{hashlib.sha256(target.encode()).hexdigest()[:8]}"
    report = _earshot(["forge", recipe, "--target", target, "--budget", str(FORGE_BUDGET), "--keep", str(FORGE_KEEP),
                       "--dir", str(out)], FORGE_TIMEOUT_S)
    made = []
    for p in report.get("picks", []):
        if not p.get("file") or not Path(p["file"]).exists():
            continue
        wav = Path(p["file"])
        made.append(_append({"id": f"forge:{out.name}:{p['name']}", "source": "earshot-forge", "provider": "earshot",
                             "license": "code-made", "title": clean_title(f"{recipe} {target}"), "tags": [recipe],
                             "recipe": recipe, "target": target, "path": wav.resolve().relative_to(lib_dir().resolve()).as_posix(),
                             "duration_s": round(seconds(wav), 3), "peak_s": None,
                             "sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
                             "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
    if not made:
        raise SoundRefused("forge_empty", "earshot forge returned no picks")
    return {"made": [{"id": r["id"], "duration_s": r["duration_s"], "license": "code-made"} for r in made]}


def match(sound_id: str, description: str) -> dict:
    """How well a library sound fits a description: earshot's calibrated z and verdict."""
    r = _earshot(["match", str(path(sound_id)), clean_title(description) or "a sound"], 600)
    return {k: r.get(k) for k in ("query", "z", "verdict", "percentile")}
