"""Story-first briefs from the channel's psychology playbook (Plan 3a Task 3; design 8.5 item 17; FAILURE_MODES.md,
topics 5-13). One no-tools Claude call writes the ideas, each naming its lever. Code clamps the rubric, rejects blocked
words, unknown levers and repeats, queues the rest and records every pass, skipped ones included."""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import OUT
from ..claude_call import SENSOR, Refused, call
from ..workspace import blocked, format_of
from . import store

PARTS = {"relatable": 25, "stakes": 20, "curiosity": 20, "novelty": 20, "fit": 15}  # the playbook's rubric
TEXT = ("key", "moment", "angle", "replay", "lever")
SCHEMA = {"type": "object", "required": ["briefs"], "properties": {"briefs": {"type": "array", "items": {
    "type": "object", "required": [*TEXT, "timely", "scores"],
    "properties": {**{k: {"type": "string"} for k in TEXT}, "timely": {"type": "boolean"},
                   "scores": {"type": "object", "required": list(PARTS),
                              "properties": {k: {"type": "number"} for k in PARTS}}}}}}}
HISTORY, OVERLAP, CROSS_DAYS = 60, 0.7, 30


class _Full(Exception):
    """The backlog already covers its days: the pass is skipped before any call."""


def _overlap(a: set[str], b: set[str]) -> float:
    # ponytail: shared content words over the smaller key; synonyms with no shared word slip through (the prompt's
    # history is the first guard; embeddings if repeats get past both)
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _judge(b, ws: dict, seen: list, cross: list) -> tuple[str | None, dict, float]:
    """(the rejection reason or None, the clamped parts, the score) for one brief."""
    if not (isinstance(b, dict) and all(isinstance(b.get(k), str) and b[k].strip() for k in TEXT)
            and isinstance(b.get("scores"), dict) and all(type(b["scores"].get(k)) in (int, float) for k in PARTS)):
        return "malformed: a field is missing or of the wrong type", {}, 0.0
    parts = {k: max(0.0, min(float(b["scores"][k]), cap)) for k, cap in PARTS.items()}
    score = round(sum(parts.values()), 1)
    text = " ".join(b[k] for k in ("key", "moment", "angle", "replay"))
    if hit := blocked(text, ws["blocked_words"]):
        return f"blocked word {hit[0]!r}", parts, score
    if b["lever"].strip().lower() not in {x.lower() for x in ws["topics"]["levers"]}:
        return f"unknown lever {b['lever']!r}", parts, score
    words = set(store.topic_key(b["key"]).split())
    for other, label in seen:
        if _overlap(words, other) >= OVERLAP:
            return f"repeat of {label!r}", parts, score
    for other, channel, kind, at in cross:
        if _overlap(words, other) >= OVERLAP:
            return f"cross-channel repeat ({channel}, {kind} {at[:10]})", parts, score
    if score < ws["topics"].get("min_score", 60):
        return f"below min_score ({score} < {ws['topics'].get('min_score', 60)})", parts, score
    return None, parts, score


def write_briefs(conn, ws: dict, now: datetime, *, finished: int = 0, claude_bin: list[str] | None = None,
                 sensor: Path | None = None, root: Path = OUT) -> dict:
    """One topic pass for one channel: {queued: [brief ids], rejected: [{key, reason}], skipped: reason or None,
    file}. finished: videos already made and waiting for a slot. A full backlog skips before the call; a refused or
    malformed call skips the pass and returns normally; it never raises into the tick."""
    name, tp = ws["name"], ws["topics"]
    rank = {"min_score": tp.get("min_score", 60), "half_life_days": tp.get("timely_half_life_days", 14)}
    own = [(r["topic_key"], json.loads(r["brief"])) for r in conn.execute(
        "select topic_key, brief from briefs where workspace = ? order by id desc limit ?", (name, HISTORY))]
    published = [r["topic_key"] for r in conn.execute(
        "select topic_key from published_topics where workspace = ? order by at desc limit ?", (name, HISTORY))]
    seen = [(set(k.split()), b.get("key", k)) for k, b in own] + [(set(k.split()), k) for k in published]
    # a topic removed in the console stays removed (9 Nov), however old: past HISTORY too
    seen += [(set(r["topic_key"].split()), r["key"]) for r in conn.execute(
        "select topic_key, key from briefs where workspace = ? and status = 'removed'", (name,))]
    recent = now - timedelta(days=CROSS_DAYS)
    cross = [(set(r["topic_key"].split()), r["workspace"], "published", r["at"]) for r in conn.execute(
        "select * from published_topics where workspace != ?", (name,)) if datetime.fromisoformat(r["at"]) >= recent]
    cross += [(set(r["topic_key"].split()), r["workspace"], r["status"], r["created_at"]) for r in conn.execute(
        "select * from briefs where workspace != ? and status in ('queued', 'used')", (name,))
        if datetime.fromisoformat(r["created_at"]) >= recent]
    sc = ws.get("schedule") or {}
    room = (sc.get("backlog_days", 14) * len(sc.get("slots", [])) - finished - len(store.live(conn, name, now, **rank))
            if sc else None)  # no schedule: a production-only channel has no slots to cover
    history = ([f"- {b.get('key', k)} ({b.get('lever', '?')})" for k, b in own] + [f"- {k}" for k in published])
    prompt = (f"Write {tp.get('ideas_per_pass', 6)} briefs for new {ws['display_name']} {format_of(ws)['label']}s, as "
              "your system prompt describes.\n\nLevers (name exactly one per brief, spelled as here):\n"
              + "\n".join(f"- {x}" for x in tp["levers"])
              + "\n\nRecent topics (never repeat one, even in new words):\n" + ("\n".join(history) or "- none yet"))
    out, record = {"queued": [], "rejected": [], "skipped": None}, {"at": now.isoformat(), "workspace": name,
                                                                    "call": None, "briefs": []}
    items = []
    try:
        if room is not None and room <= 0:
            raise _Full(f"backlog full: live briefs and {finished} finished video(s) cover "
                        f"{sc.get('backlog_days', 14)} days of slots")
        with tempfile.TemporaryDirectory() as td:
            r = call("topics", prompt, cwd=Path(td), schema=SCHEMA, claude_bin=claude_bin, sensor=sensor or SENSOR,
                     system_file=Path(ws["dir"]) / ws["playbooks"]["topics"], usage_log=root / "usage.jsonl")
        record["call"] = {"exit": r.exit_code, "seconds": r.seconds, "billing": r.billing, "timed_out": r.timed_out,
                          "isolation": r.isolation}
        problems = r.isolation + (["timed out"] if r.timed_out else []) + ([f"exit {r.exit_code}"] if r.exit_code else [])
        items = (r.data or {}).get("briefs")
        if problems or not isinstance(items, list):
            out["skipped"], items = "malformed reply: " + ("; ".join(problems) or "no briefs list"), []
    except Refused as e:
        out["skipped"] = f"quota: {e}"
    except _Full as e:
        out["skipped"] = str(e)
    accepted = []
    for b in items:
        reason, parts, score = _judge(b, ws, seen, cross)
        entry = {**b, "raw_scores": b.get("scores"), "parts": parts, "score": score} if isinstance(b, dict) else {"raw": b}
        record["briefs"].append(entry)
        if reason is None:
            seen.append((set(store.topic_key(b["key"]).split()), b["key"]))  # in-batch repeats
            accepted.append((score, b, entry))
        else:
            out["rejected"].append({"key": b.get("key") if isinstance(b, dict) else None, "reason": reason})
            entry["verdict"] = reason
    for n, (score, b, entry) in enumerate(sorted(accepted, key=lambda a: -a[0])):  # the best ones take the room
        if room is not None and n >= room:
            entry["verdict"] = f"backlog full (room for {room})"
            out["rejected"].append({"key": b["key"], "reason": entry["verdict"]})
            continue
        lever = next(x for x in tp["levers"] if x.lower() == b["lever"].strip().lower())
        bid = store.queue_brief(conn, name, {**{k: b[k] for k in TEXT}, "lever": lever, "timely": bool(b.get("timely")),
                                             "score": score, "parts": entry["parts"]}, now)
        out["queued"].append(bid)
        entry["verdict"] = f"queued #{bid}"
    folder = root / "topics" / name
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    f.write_text(json.dumps({**record, "skipped": out["skipped"]}, indent=2), encoding="utf-8")
    return {**out, "file": str(f)}
