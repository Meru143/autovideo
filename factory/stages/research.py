"""Researcher: a Claude call with web search and fetch only finds today's sources for a brief (design 8.5 item 10,
R9 Q69; Plan 2b Task 3b; FAILURE_MODES.md, research). It has no terminal, no file writing and no secrets; code checks
its link list, and fetch_sources saves the copies the script may quote."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ..claude_call import SENSOR, call
from ..jobs import Job, Quarantined
from ..workspace import load

SCHEMA = {"type": "object", "required": ["sources"], "properties": {
    "sources": {"type": "array", "items": {"type": "object", "required": ["id", "url", "why", "group"], "properties": {
        "id": {"type": "string"}, "url": {"type": "string"}, "why": {"type": "string"}, "group": {"type": "string"},
        "published": {"type": "string"}}}},
    "notes": {"type": "string"}}}
CREATOR_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "x.com", "twitter.com")
MAX_SOURCES = 6


def _usable(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and bool(host) and not any(host == h or host.endswith("." + h) for h in CREATOR_HOSTS)


def research(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    ws = load(job.workspace)
    work = job.dir / "research"
    work.mkdir(exist_ok=True)
    shutil.copyfile(job.brief, work / "brief.md")  # the researcher's folder holds only the brief
    r = call("research", "Find the sources for this brief.\n\n" + job.brief.read_text(encoding="utf-8"), cwd=work,
             tools=("WebSearch", "WebFetch"), schema=SCHEMA, system_file=Path(ws["dir"]) / ws["playbooks"]["research"],
             claude_bin=claude_bin, usage_log=job.dir / "usage.jsonl", sensor=sensor)
    if r.isolation:
        raise RuntimeError(f"research run not isolated, output unused: {r.isolation}")
    if r.data is None:
        raise RuntimeError(f"research returned no valid structured output (exit {r.exit_code}, timed out {r.timed_out})")
    kept = [s for s in r.data["sources"] if _usable(s.get("url", ""))][:MAX_SOURCES]
    # code numbers the sources: a model's ID ("..\x", "doi:10.1/x", a duplicate) never names a file (final review I5)
    sources = [{**s, "id": f"s{i}"} for i, s in enumerate(kept, 1)]
    dropped = [s for s in r.data["sources"] if not _usable(s.get("url", ""))]
    groups = {" ".join(s["group"].lower().split()) for s in sources}
    (job.dir / "research.json").write_text(json.dumps({"sources": sources, "notes": r.data.get("notes", ""),
                                                       "dropped": dropped, "cost_usd": r.cost_usd}, indent=2),
                                           encoding="utf-8")
    if len(groups) < 2:
        raise Quarantined(f"research found {len(sources)} usable sources in {len(groups)} independent group(s); need 2")
    return {"sources": len(sources), "groups": len(groups), "dropped": len(dropped), "cost_usd": r.cost_usd}
