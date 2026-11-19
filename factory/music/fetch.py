"""One-time fetch of the music instruments (Plan 2c Task 1; design 6b licence gate), run by hand on a fresh clone:
    .venv/Scripts/python -m factory.music.fetch
Tone.js 15.1.22's browser build from npm, and the soft layer of FreePats' Upright Piano KW (CC0 1.0) at minor thirds
from C2 to C6. Both archives are pinned by sha256, and the lock records every kept file's sha256 with its licence."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .. import ROOT
from .instruments import LOCK, verify

HERE = Path(__file__).parent
SOURCES = {
    "tone": {"source_url": "https://registry.npmjs.org/tone/-/tone-15.1.22.tgz",
             "page": "https://www.npmjs.com/package/tone/v/15.1.22",
             "archive_sha256": "86dec0a9521dd69a59e7b824a83b132afd1ef8284bda1e7799449ab9d84d9f62",
             "licence": "MIT License, Copyright (c) 2014-2020 Yotam Mann (package LICENSE.md; build header: MIT)",
             "uploader": "npm package tone (Tone.js, Yotam Mann)", "read_on": "2026-11-05"},
    "upright_kw": {"source_url": "https://freepats.zenvoid.org/Piano/UprightPianoKW/UprightPianoKW-SFZ+FLAC-20220221.7z",
                   "page": "https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html",
                   "archive_sha256": "3a66da3b25a2fd9499d90bcf09f1aee4431825796e249361b48af59b1201e703",
                   "licence": "Published under the terms of the Creative Commons CC0 1.0 public domain dedication.",
                   "uploader": "FreePats project (recorded by Gonzalo and Roberto, 2017)", "read_on": "2026-11-05"},
}
NOTES = [f"{n}{o}" for o in range(2, 6) for n in ("C", "D#", "F#", "A")] + ["C6"]  # the soft (vL) layer
TONE_FILES = {"package/build/Tone.js": "vendor/Tone.js", "package/build/Tone.js.LICENSE.txt": "vendor/Tone.js.LICENSE.txt",
              "package/LICENSE.md": "vendor/LICENSE.tone"}


def _get(source: dict, into: Path) -> Path:
    path = into / source["source_url"].rsplit("/", 1)[1]
    with urllib.request.urlopen(source["source_url"], timeout=300) as r:
        path.write_bytes(r.read())
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != source["archive_sha256"]:
        raise SystemExit(f"{source['source_url']}: sha256 {got} is not the pinned {source['archive_sha256']}")
    return path


def _entry(source: dict, files: dict[str, Path]) -> dict:
    return {**source, "files": {name: {"path": p.relative_to(ROOT).as_posix(),
                                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for name, p in files.items()}}


def main() -> None:
    lock = {}
    with tempfile.TemporaryDirectory() as td:  # downloads are data: unpacked in their own folder, nothing run from it
        tmp = Path(td)
        with tarfile.open(_get(SOURCES["tone"], tmp)) as tar:  # only the three named members, to our own paths
            for member, rel in TONE_FILES.items():
                (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
                (HERE / rel).write_bytes(tar.extractfile(member).read())
        lock["tone"] = _entry(SOURCES["tone"], {"tone": HERE / "vendor" / "Tone.js"})

        bsdtar = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "tar.exe"  # reads 7z (libarchive)
        subprocess.run([str(bsdtar), "-xf", str(_get(SOURCES["upright_kw"], tmp)), "-C", td], check=True)
        samples, out = next(tmp.glob("UprightPianoKW-*/samples")), HERE / "samples" / "upright_kw"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        files = {}
        for note in NOTES:  # "#" would start a URL fragment in the harness, so D#4 is stored as Ds4.flac
            files[note] = out / f"{note.replace('#', 's')}.flac"
            shutil.copyfile(samples / f"{note}vL.flac", files[note])
        lock["upright_kw"] = _entry(SOURCES["upright_kw"], files)
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"verified {len(verify())} files -> {LOCK.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
