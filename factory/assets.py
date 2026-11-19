"""The channel's growing asset library (operator, 6 Nov: "keeps saving reusable assets so future runs get less n less
expensive"; FAILURE_MODES.md, assets). A scene writer saves each new set or prop it draws as its own file under the
project's new/ folder, listed in new/manifest.json. After the final review passes, harvest() files the clean ones into
the channel's brand/library/ and rewrites library/catalog.md, which the next writer reads before drawing."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("set", "prop")
MAX_BYTES = 300_000
NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
BANNED = re.compile(r"<script|<foreignObject|<image\b|href\s*=\s*[\"']https?:|url\(\s*[\"']?https?:|javascript:"
                    r"|data-layout-ignore", re.I)
IDS = re.compile(r"\bid\s*=\s*[\"']([^\"']+)[\"']")


def _refuse(it: dict, new: Path, known_shas: set[str]) -> tuple[str | None, str, str]:
    """(why it's refused or None, the file's text, its sha256), checks in order: kind, name, file, size, svg, banned
    content, duplicate, ids prefixed with the name (a set's ids start with its name; a prop's with prop-<name>)."""
    kind, name = it.get("kind"), str(it.get("name", ""))
    if kind not in KINDS:
        return f"kind must be set or prop, not {kind!r}", "", ""
    if not NAME.fullmatch(name):
        return "name must be lowercase letters, digits and hyphens", "", ""
    f = new / f"{kind}s" / f"{name}.svg"
    if not f.exists():
        return f"no file new/{kind}s/{name}.svg", "", ""
    text = f.read_text(encoding="utf-8", errors="replace")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if len(text.encode("utf-8")) > MAX_BYTES:
        return "over 300 KB", text, sha
    if "<svg" not in text[:400] or "</svg>" not in text:
        return "not an SVG file", text, sha
    if m := BANNED.search(text):
        return f"not allowed in a library asset: {m.group(0)!r}", text, sha
    if sha in known_shas:
        return "already in the library", text, sha
    prefix = name if kind == "set" else f"prop-{name}"
    if bad := [i for i in IDS.findall(text) if not i.startswith(prefix)]:
        return f"ids must start with {prefix!r} so two assets can share a frame: {bad[:3]}", text, sha
    return None, text, sha


def harvest(project: Path, ws: dict, job_id: str, now: datetime | None = None) -> dict:
    """File a passing video's new sets and props into the channel library; returns {harvested, skipped}."""
    mf = project / "new" / "manifest.json"
    if not mf.exists():
        return {"harvested": [], "skipped": []}
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
        items = data["assets"] if isinstance(data, dict) else data
        assert isinstance(items, list)
    except (ValueError, KeyError, TypeError, AssertionError):
        return {"harvested": [], "skipped": [{"name": None, "why": "new/manifest.json is not a valid asset list"}]}
    lib = Path(os.environ.get("FACTORY_LIBRARY_ROOT") or Path(ws["dir"]) / "brand") / "library"  # env: tests
    index = lib / "manifest.jsonl"
    rows = [json.loads(ln) for ln in index.read_text(encoding="utf-8").splitlines()] if index.exists() else []
    shas, names = {r["sha256"] for r in rows}, {r["name"] for r in rows}
    out = {"harvested": [], "skipped": []}
    for it in (i for i in items if isinstance(i, dict)):
        why, text, sha = _refuse(it, project / "new", shas)
        if why:
            out["skipped"].append({"name": it.get("name"), "why": why})
            continue
        kind, name = it["kind"], it["name"]
        final = name if name not in names else f"{name}-{sha[:6]}"  # ponytail: ids keep the old name; rewrite if clashes bite
        dest = lib / f"{kind}s" / f"{final}.svg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        row = {"name": final, "kind": kind, "description": str(it.get("description", ""))[:200],
               "tags": [str(t)[:30] for t in it.get("tags", []) if isinstance(t, str)][:8],
               "file": f"library/{kind}s/{final}.svg", "job": job_id,
               "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "sha256": sha}
        with index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        rows.append(row)
        shas.add(sha)
        names.add(final)
        out["harvested"].append(final)
    _catalog(lib, rows)
    return out


def _catalog(lib: Path, rows: list[dict]) -> None:
    lines = ["# Asset library", "", "Sets and props earlier videos drew, ready to reuse as they are. Check here before "
             "drawing anything new: reuse is free, drawing costs time and money.", ""]
    for kind in KINDS:
        if mine := [r for r in rows if r["kind"] == kind]:
            lines += [f"## {kind.title()}s", ""]
            lines += [f"- `{r['file']}`: {r['description']}" + (f" ({', '.join(r['tags'])})" if r["tags"] else "")
                      for r in mine] + [""]
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "catalog.md").write_text("\n".join(lines), encoding="utf-8")
