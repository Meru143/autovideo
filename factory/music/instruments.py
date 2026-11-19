"""The engine and samples a composition may load (Plan 2c; design 6b licence gate). Each file is pinned by sha256 in
instruments.lock.json beside the licence as read on its source page; the harness serves nothing else."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import ROOT

LOCK = Path(__file__).with_name("instruments.lock.json")


class InstrumentMissing(RuntimeError):
    pass


def verify(lock: Path = LOCK) -> dict[str, Path]:
    """{name: path} for every locked file (sample names such as "C4", and "tone"), or InstrumentMissing naming the
    first file that is missing, altered or without a recorded licence."""
    found = {}
    for source, entry in json.loads(lock.read_text(encoding="utf-8")).items():
        if not entry.get("licence", "").strip() or not entry.get("read_on"):
            raise InstrumentMissing(f"{source}: no licence recorded as read on its source page")
        for name, f in entry["files"].items():
            path = ROOT / f["path"]
            if not path.is_file():
                raise InstrumentMissing(f"{name} ({source}) is missing at {f['path']}; run python -m factory.music.fetch")
            if hashlib.sha256(path.read_bytes()).hexdigest() != f["sha256"]:
                raise InstrumentMissing(f"{name} ({source}) doesn't match its locked sha256: {f['path']}")
            found[name] = path
    return found
