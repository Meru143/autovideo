"""A story channel's asset library (engine plan): what earlier reels built, by kind and name with a description, in
the channel's brand/library/index.json. A reel uses a library item only when it is the same thing it needs, and each
passing reel's new assets are saved into it (the owner, 10 Nov: "and save those assets too")."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from . import ROOT
from .jobs import Job

AUTHOR = "factory library <library@autovideo.invalid>"
KINDS = ("character", "prop", "pose", "set")
CLIP_M = 0.03  # the kit check's line: a clip deeper than 3 cm shows


def _lib(ws: dict) -> Path:
    return Path(ws["dir"]) / "brand" / "library"


def index(ws: dict) -> list[dict]:
    """The library's items, [{kind, name, description, job, saved}]; [] with no library yet."""
    f = _lib(ws) / "index.json"
    return json.loads(f.read_text(encoding="utf-8")).get("items", []) if f.is_file() else []


def _free(folder: Path, name: str, taken: set[str]) -> str:
    """name, or name_2, name_3 ... : the first one no file in the library (or this save) holds. Never overwrites."""
    n, i = name, 1
    while (folder / f"{n}.js").exists() or n in taken:
        i += 1
        n = f"{name}_{i}"
    return n


# the asset's own default export as the kit check will define it: E.define(a.kind, a.name, a.build)
META_JS = """const a = (await import(process.argv[1])).default || {};
console.log(JSON.stringify({ kind: a.kind, name: a.name, description: a.description }));"""


def _inspect(brand: Path, kind: str, name: str, src: str) -> tuple[dict, list[str]]:
    """The asset's {kind, name, description} as its file exports them, and checkpass.mjs --kit's faults on the kit
    with this one asset defined: clips over 3 cm, hair over a face, errors. The base kit checks clean (kit_clean), so
    anything the run reports is the asset's."""
    from .render import _hf, _run, install_vendor
    # ponytail: one node run per asset (a held prop's clip names its holder, not the prop); batch if saves get slow
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        shutil.copytree(brand, proj / "brand", ignore=shutil.ignore_patterns("*.py", "__pycache__", "library"))
        install_vendor(proj)
        f = proj / "assets" / kind / f"{name}.js"
        f.parent.mkdir(parents=True)
        f.write_text(src, encoding="utf-8")
        m = _run([_hf()[0], "--input-type=module", "-e", META_JS, f.as_uri()], cwd=proj, timeout_s=60)
        try:
            meta = json.loads(m.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}, [f"the asset doesn't load: {(m.stderr or m.stdout or '').strip()[-300:]}"]
        if (meta.get("kind"), meta.get("name")) != (kind, name):
            return meta, [f"it registers as {meta.get('kind')}/{meta.get('name')}, but would be saved as {kind}/{name}"]
        r = _run([_hf()[0], str(ROOT / "factory" / "checkpass.mjs"), str(proj), "--kit", "--out", str(proj / "kit.json")],
                 cwd=proj, timeout_s=900)
        try:
            rep = json.loads((proj / "kit.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return meta, [f"the kit check crashed (exit {r.returncode}): {(r.stderr or r.stdout or '').strip()[-300:]}"]
    return meta, ([f"clip: {h['what']}: {h['owner']}/{h['part']} into {h['other']} {h['depthM']:.3f} m" for h in rep["hits"]
                   if h["depthM"] > CLIP_M] + [f"over the face: {f}" for f in rep["face"]] + [f"error: {e}" for e in rep["errors"]])


def fetch(project: Path, ws: dict, assets: list[dict]) -> list[str]:
    """Copy each shot-table asset marked "library" that the library index holds into project/assets/<kind>/<name>.js,
    where a reel's own assets live (their ../../brand/ imports resolve from there). Base-kit pieces need no copy. An
    asset the index lists whose file is gone raises: the writer would import a file that isn't there."""
    held, got = {(i.get("kind"), i.get("name")) for i in index(ws)}, []
    for a in assets:
        kind, name = a.get("kind"), a.get("name")
        src = _lib(ws) / str(kind) / f"{name}.js"
        if a.get("source") == "library" and (kind, name) in held and kind in KINDS:
            if not src.is_file():
                raise RuntimeError(f"the library's index lists {kind}/{name}, but its file {src} is gone: "
                                   "restore it or drop it from index.json")
            (project / "assets" / kind).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, project / "assets" / kind / f"{name}.js")
            got.append(f"{kind}/{name}")
    return got


def save(job: Job, ws: dict, *, repo: Path = ROOT) -> list[str]:
    """Save a passing reel's new assets (project/assets/<kind>/<name>.js) into the library and commit them, authored
    factory library. A name the library holds already is saved as <name>_2, <name>_3 ...; an asset the shot table
    marks "library" is the library's own and is skipped. An asset isn't saved when its file's description is empty or
    differs from the shot table's, when it registers under another name than the one it would be saved as, or when the
    kit check finds a clip in it. A job the index already lists was saved (a retry): nothing again. A dirty index.json
    (someone's uncommitted edit) saves nothing; a failed commit puts the files back and raises. Returns the saved
    "kind/name"."""
    src = job.dir / "project" / "assets"
    files = sorted(p for k in KINDS for p in (src / k).glob("*.js")) if src.is_dir() else []
    lib, refused = _lib(ws), []
    idx = lib / "index.json"
    if done := [f"{i['kind']}/{i['name']}" for i in index(ws) if i.get("job") == job.id]:
        return done  # the commit landed and the stage died before its marker: never save the same assets twice
    if not files:
        return []
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,  # noqa: E731
                                    timeout=120)
    if git("status", "--porcelain", "--", str(idx)).stdout.strip():
        _record(job, [], [{"asset": "*", "why": "the library's index.json has uncommitted edits"}])
        return []
    try:
        table = json.loads((job.dir / "shot_table.json").read_text(encoding="utf-8")).get("assets", [])
    except (OSError, ValueError, AttributeError):
        table = []
    desc = {(a.get("kind"), a.get("name")): str(a.get("description") or "").strip()
            for a in table if a.get("source") == "new"}
    borrowed = {(a.get("kind"), a.get("name")) for a in table if a.get("source") == "library"}
    brand, new, taken = Path(ws["dir"]) / "brand", [], set()
    for p in files:
        kind, name = p.parent.name, p.stem
        if (kind, name) in borrowed:
            continue
        n = _free(lib / kind, name, taken)
        text = p.read_text(encoding="utf-8")
        if n != name:  # the saved file names itself as saved: E.define(kind, asset.name, ...)
            text = re.sub(rf"""(\bname\s*:\s*)(["']){re.escape(name)}\2""", rf"\g<1>\g<2>{n}\g<2>", text, count=1)
        meta, faults = _inspect(brand, kind, n, text)
        own, told = str(meta.get("description") or "").strip(), desc.get((kind, name), "")
        if not faults and (not own or own != told):
            faults = [f"its description {own!r} differs from the shot table's {told!r}" if own else
                      "its file has no description"]
        if faults:
            refused.append({"asset": f"{kind}/{name}", "why": faults[:5]})
            continue
        taken.add(n)
        new.append((lib / kind / f"{n}.js", text, {"kind": kind, "name": n, "description": own,
                                                    "job": job.id, "saved": date.today().isoformat()}))
    if new:
        old = idx.read_bytes() if idx.is_file() else None
        items = index(ws) + [e for _, _, e in new]
        for f, text, _ in new:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8", newline="\n")
        idx.write_text(json.dumps({"items": items}, indent=2), encoding="utf-8", newline="\n")
        paths = [str(f) for f, _, _ in new] + [str(idx)]
        g = git("add", "--", *paths)
        if g.returncode == 0:
            g = git("commit", "--author", AUTHOR, "-m", f"library({job.workspace}): {len(new)} assets from {job.id}",
                    "--", *paths)
        if g.returncode != 0:  # git busy or refusing: the library goes back as it was
            git("reset", "-q", "--", *paths)
            for f, _, _ in new:
                f.unlink(missing_ok=True)
            idx.write_bytes(old) if old is not None else idx.unlink(missing_ok=True)
            raise RuntimeError(f"library commit failed, files restored: {(g.stderr or g.stdout)[-300:]}")
    saved = [f"{e['kind']}/{e['name']}" for _, _, e in new]
    _record(job, saved, refused)
    return saved


def _record(job: Job, saved: list[str], refused: list[dict], error: str | None = None) -> None:
    """checks/library.json: what the save kept and what it refused, and why; a save that raised, its error."""
    out = job.dir / "checks" / "library.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"saved": saved, "refused": refused, **({"error": error} if error else {})}, indent=2),
                   encoding="utf-8")
