"""A story channel's kit inventory (story plan Task 3; FAILURE_MODES.md, kit). The kit follows the stories: what the
approved shot tables use (locations, characters, poses, expressions, props, sound events, camera moves and framings)
is what the cast and set builds make, and nothing more. A later story's gaps are named before any render."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

KINDS = ("locations", "characters", "poses", "expressions", "props", "events", "cameras", "framings")


def name(text: str) -> str:
    """One spelling for one thing: lowercase words joined by "_", articles dropped, a plural "s" trimmed."""
    words = [w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in ("a", "an", "the")]
    return "_".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words)


def inventory(shot_tables: list[list[dict]]) -> dict:
    """{kind: {name: {count, uses, characters?, times?}}} over every shot; uses are "t<table>s<shot>". Raises
    ValueError for no shots at all."""
    inv: dict[str, dict] = {k: {} for k in KINDS}

    def add(kind: str, raw: str, use: str, who: str | None = None, when: str | None = None) -> None:
        if not (key := name(raw)):
            return
        e = inv[kind].setdefault(key, {"count": 0, "uses": []})
        e["count"] += 1
        e["uses"].append(use)
        if who is not None:
            e["characters"] = sorted(set(e.get("characters", [])) | {who})
        if when is not None:
            e["times"] = sorted(set(e.get("times", [])) | {when})
    for t, table in enumerate(shot_tables):
        for s, shot in enumerate(table):
            use = f"t{t}s{s}"
            add("locations", shot.get("location", ""), use, when=shot.get("time"))
            add("cameras", shot.get("camera", ""), use)
            add("framings", shot.get("framing", ""), use)
            for ev in shot.get("events", []):
                add("events", ev, use)
            for c in shot.get("characters", []):
                who = name(c.get("id", ""))
                add("characters", who, use)
                add("poses", c.get("pose", ""), use, who)
                add("expressions", c.get("expression", ""), use, who)
                for p in c.get("props", []):
                    add("props", p, use, who)
    if not any(inv[k] for k in KINDS):
        raise ValueError("empty inventory: no shots to build a kit for")
    return inv


# an asset's kind in a shot table's assets list, and the base kit's inventory kind that holds it: what a render can't
# do without. An expression, sound or framing falls back (skipped and logged); a camera move is the film writer's.
KIT_KIND = {"character": "characters", "set": "locations", "pose": "poses", "prop": "props"}


def missing(assets: list[dict], kit: dict, library: list[dict] = ()) -> list[str]:
    """The shot table's "library" assets that neither the base kit nor the library holds, as "kind: name", by exact
    name. A "new" asset is the reel's own to build, never a gap (engine Task 5: own assets first, the library only for
    the same thing). A base-kit character, set or prop is the same thing only by its description (its `desc`), so one
    without is a gap too, saying so; a pose is a building block, matched by name."""
    have, gaps = {(i.get("kind"), i.get("name")) for i in library}, set()
    for a in assets:
        kind, n = a.get("kind"), a.get("name", "")
        if a.get("source") != "library" or (kind, n) in have:
            continue
        entry = kit.get(KIT_KIND.get(kind, ""), {}).get(n)
        if entry is None:
            gaps.add(f"{kind}: {n}")
        elif kind != "pose" and not str(entry.get("desc") or "").strip():
            gaps.add(f"{kind}: {n} (in the kit with no description, so it can't be judged the same thing)")
    return sorted(gaps)


# the kit's own one-line looks: cast.js CHARACTERS[id].desc and PROP_DESC[name], sets.js LOCATIONS[name].desc
DESCRIBE_JS = """const u = process.argv[1], C = await import(u + "/cast.js"), S = await import(u + "/sets.js");
const d = (o) => Object.fromEntries(Object.entries(o || {}).map(([k, v]) => [k, typeof v === "string" ? v : v && v.desc])
  .filter(([, v]) => v));
console.log(JSON.stringify({ characters: d(C.CHARACTERS), locations: d(S.LOCATIONS), props: d(C.PROP_DESC) }));"""


def describe(brand: Path, module: Path | None = None) -> dict:
    """{characters, locations, props: {name: desc}} from a kit's cast.js and sets.js, read by node on a scratch copy
    beside three.js (engine.js imports it from ../vendor); module, a rebuilt cast.js or sets.js, stands in for the kit's
    own. Raises when the kit doesn't load."""
    from .render import _hf, _run, install_vendor
    with tempfile.TemporaryDirectory() as d:
        shutil.copytree(brand, Path(d) / "brand", ignore=shutil.ignore_patterns("*.py", "__pycache__", "library"))
        if module is not None:
            shutil.copyfile(module, Path(d) / "brand" / module.name)
        install_vendor(Path(d))
        r = _run([_hf()[0], "--input-type=module", "-e", DESCRIBE_JS, (Path(d) / "brand").as_uri()], cwd=Path(d),
                 timeout_s=60)
    if r.returncode != 0:
        raise RuntimeError(f"the kit's descriptions didn't load: {(r.stderr or r.stdout).strip()[-400:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def add_descriptions(brand: Path, looks: dict | None = None) -> dict:
    """Carry the kit's descriptions (looks, or read from the kit) into brand/inventory.json, the inventory the shots
    stage reads; returns them."""
    looks, f = looks if looks is not None else describe(brand), brand / "inventory.json"
    inv = json.loads(f.read_text(encoding="utf-8"))
    for kind, names in looks.items():
        for n, e in inv.get(kind, {}).items():
            e.pop("desc", None)
            if n in names:
                e["desc"] = names[n]
    f.write_text(json.dumps(inv, indent=2), encoding="utf-8", newline="\n")
    return looks


def summary(inv: dict) -> str:
    """The short list the operator approves before any build."""
    def few(kind: str) -> str:
        names = sorted(inv[kind], key=lambda n: -inv[kind][n]["count"])
        return ", ".join(n.replace("_", " ") for n in names[:8]) + (", ..." if len(names) > 8 else "")
    n = {k: len(inv[k]) for k in KINDS}
    return (f"{n['locations']} locations ({few('locations')}); {n['characters']} characters ({few('characters')}); "
            f"{n['poses']} poses; {n['expressions']} expressions; {n['props']} props ({few('props')}); "
            f"{n['events']} sound kinds ({few('events')}); {n['cameras']} camera moves ({few('cameras')})")
