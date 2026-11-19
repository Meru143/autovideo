"""The learning loop's proposal (Plan 3b Task 3): one call with no tools reads the evidence and the channel's
editable playbooks and settings, and proposes exactly one change to raise day-7 retention. Code refuses anything
outside the allowlist, a no-op, a gutted playbook, a changed type or a malformed reply, by name
(FAILURE_MODES.md, loop). Per format (ruling 4, 9 Nov): a story channel may change its topics only."""
from __future__ import annotations

import json
from pathlib import Path

from .. import OUT
from ..claude_call import SENSOR, call
from ..workspace import format_of

PLAYBOOKS = {"explainer": ("topics", "script"), "story": ("topics",)}  # metadata: once something benchmarks it
TOPICS = ("topics.levers", "topics.ideas_per_pass", "topics.min_score")
SETTINGS = {"explainer": (*TOPICS, "script_rules."), "story": TOPICS}  # a trailing dot allows its keys
MAX_CUT = 0.4  # a rewrite may drop at most this share of a playbook's text
SCHEMA = {"type": "object", "required": ["target", "path", "change", "rationale", "expected"], "properties": {
    "target": {"type": "string", "enum": ["playbook", "setting"]}, "path": {"type": "string"}, "change": {},
    "rationale": {"type": "string"}, "expected": {"type": "string"}}}


def allowlist(ws: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    kind = format_of(ws)["pipeline"]
    return PLAYBOOKS.get(kind, ()), SETTINGS.get(kind, ())


def _setting(ws: dict, key: str):
    v = ws
    for part in key.split("."):
        v = v[part]
    return v


def _settings(ws: dict, keys: tuple[str, ...]) -> dict:
    out = {}
    for k in keys:
        try:
            out[k.rstrip(".")] = _setting(ws, k.rstrip("."))
        except (KeyError, TypeError):
            continue
    return out


def refusal(ws: dict, p: dict) -> str | None:
    """Why this proposal is refused, or None when it may go to the benchmark."""
    books, keys = allowlist(ws)
    path = str(p.get("path", ""))
    if p.get("target") == "playbook":
        name = Path(path).stem
        if path not in (name, f"{name}.md", f"playbooks/{name}.md") or name not in books or name not in ws["playbooks"]:
            return f"outside the allowlist: {path!r} (playbooks {list(books)})"
        if not isinstance(p.get("change"), str):
            return "a playbook change must be its full new text"
        old = (Path(ws["dir"]) / ws["playbooks"][name]).read_text(encoding="utf-8")
        if p["change"].strip() == old.strip():
            return "a no-op: the playbook is unchanged"
        if len(p["change"]) < (1 - MAX_CUT) * len(old):
            return f"the playbook would lose {1 - len(p['change']) / len(old):.0%} of its text"
        return None
    if p.get("target") == "setting":
        if not any(path == k or (k.endswith(".") and path.startswith(k) and len(path) > len(k)) for k in keys):
            return f"outside the allowlist: {path!r} (settings {list(keys)})"
        try:
            old = _setting(ws, path)
        except (KeyError, TypeError):
            return f"no such setting: {path!r}"
        if p.get("change") == old:
            return "a no-op: the setting is unchanged"
        if type(p.get("change")) is not type(old):
            return f"{path} must stay a {type(old).__name__}"
        return None
    return "malformed: target must be playbook or setting"


def _prompt(ws: dict, ev: dict, books: tuple[str, ...], keys: tuple[str, ...]) -> str:
    texts = "\n\n".join(f"--- {b}.md ---\n" + (Path(ws["dir"]) / ws["playbooks"][b]).read_text(encoding="utf-8")
                          for b in books if b in ws["playbooks"])
    brief = {k: ev.get(k) for k in ("median", "mad", "levers", "top", "in_flight")}
    brief["best"] = [{k: v.get(k) for k in ("key", "lever", "retention")} for v in ev.get("best", [])]
    brief["worst"] = [{k: v.get(k) for k in ("key", "lever", "retention")} for v in ev.get("worst", [])]
    return "\n\n".join([
        f"You tune {ws.get('display_name', ws['name'])}, a channel of short videos. The score is each video's day-7 "
        "retention: the average percentage of it that viewers watched.",
        "## Evidence\n" + json.dumps(brief, indent=1, default=str),
        "## What you may change: exactly one of these\n"
        f"- a playbook, giving its full new text: {', '.join(b + '.md' for b in books)}\n"
        f"- a setting, giving its new value of the same type: {', '.join(keys)} (a trailing dot means its keys)",
        "## The playbooks now\n" + texts,
        "## The settings now\n" + json.dumps(_settings(ws, keys), indent=1),
        "Propose the one change most likely to raise day-7 retention, judging from the evidence: what the best videos "
        "share and the worst lack, and which levers hold viewers. Keep everything else as it is; a playbook keeps its "
        "rules, voice and layout. Reply with JSON: target (playbook or setting), path, change, rationale, expected."])


def propose(ws: dict, ev: dict, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR,
            cwd: Path | None = None) -> dict:
    """The proposal, or {"refused": why} (with the proposal, when one came back)."""
    books, keys = allowlist(ws)
    cwd = cwd or OUT / "loop" / ws["name"]
    cwd.mkdir(parents=True, exist_ok=True)
    r = call("loop", _prompt(ws, ev, books, keys), cwd=cwd, tools=(), schema=SCHEMA, claude_bin=claude_bin,
             sensor=sensor)
    if r.isolation:
        return {"refused": f"not isolated: {r.isolation}"}
    if not isinstance(r.data, dict):
        return {"refused": "malformed: no valid reply"}
    if why := refusal(ws, r.data):
        return {"refused": why, "proposal": r.data}
    return r.data
