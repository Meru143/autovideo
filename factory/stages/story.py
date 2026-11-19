"""story: one "In this world..." episode as data (beats with roles, and a clue ledger), checked by code (story plan
Task 2; design 5.4.2, 8.5 item 20; FAILURE_MODES.md, story). The beats are the narration: code joins them, so the voice
reads exactly what the checks read."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import OUT
from ..claude_call import SENSOR, call
from ..jobs import Job
from ..script_lint import lint
from ..topics import store
from ..workspace import blocked, format_of, load, story_rules_of

ROLES = ("rule", "stakes", "enforcer", "custom", "vow", "false_hope", "threat", "flip", "callback")
TEXT = ("rule", "title_rule", "twist_family", "situation", "outcome")
SCHEMA = {"type": "object", "required": [*TEXT, "beats", "clue_ledger"], "properties": {
    **{k: {"type": "string"} for k in TEXT},
    "beats": {"type": "array", "items": {"type": "object", "required": ["text", "role", "clues"], "properties": {
        "text": {"type": "string"}, "role": {"enum": list(ROLES)}, "clues": {"type": "array", "items": {"type": "string"}}}}},
    "clue_ledger": {"type": "array", "items": {"type": "object",
                                               "required": ["id", "planted_beat", "withheld_how", "pays_off_beat"],
                                               "properties": {"id": {"type": "string"}, "planted_beat": {"type": "integer"},
                                                              "withheld_how": {"type": "string"},
                                                              "pays_off_beat": {"type": "integer"}}}}}}
CTA = re.compile(r"\b(what would you do|would you rather|comment (?:below|down|your)|in the comments|subscribe|"
                 r"like and|follow (?:for|me)|let me know|tell me (?:in|below)|vote (?:in|below|now)|link in)\b", re.I)
OVERLAP = 0.7  # shared content words over the smaller key make a repeat, as for topic briefs


def _words(text: str) -> set[str]:
    return set(store.topic_key(text).split())


def _same(a: str, b: str) -> bool:
    x, y = _words(a), _words(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= OVERLAP


def narration_of(ep: dict) -> str:
    return " ".join(b["text"].strip() for b in ep["beats"])


def script_view(ep: dict) -> dict:
    """What the later stages read as script.json (music, metadata, review, sources): the narration, the rule and
    title as hooks, and no claims, since fiction quotes no source."""
    return {"format": "story", "narration": narration_of(ep), "hooks": [ep.get("rule", ""), ep.get("title_rule", "")],
            "beats": [], "takeaway": ep.get("rule", "")}


def examples_of(ws: dict) -> list[str]:
    """The channel's example stories (playbooks.story_examples), one text per "## " section: real viral reels the
    writer learns the voice from (operator, 6 Nov)."""
    rel = ws["playbooks"].get("story_examples")
    if not rel:
        return []
    parts = (Path(ws["dir"]) / rel).read_text(encoding="utf-8").split("\n## ")[1:]
    return [p.split("\n", 1)[1].strip() for p in parts if "\n" in p]


def _runs(text: str, n: int = 8) -> set[str]:
    """Every run of n words, lowercased, punctuation dropped: a copied line shares one."""
    w = re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def check_episode(ep: dict, ws: dict, history: list[tuple[str, str, str]]) -> list[str]:
    """The episode's failures, by name. history: the channel's other (situation, outcome, job) records."""
    sr, beats = story_rules_of(ws), ep.get("beats") or []
    if not beats or any(b.get("role") not in ROLES or not str(b.get("text", "")).strip() for b in beats):
        return ["malformed: every beat needs text and a known role"]
    failures, roles, text = [], [b["role"] for b in beats], narration_of(ep)
    if roles[0] != "rule" or not text.lstrip(" \"'“").lower().startswith(sr["opening"].lower()):
        failures.append(f"opening: the first beat is the rule, starting \"{sr['opening']}\"")
    if "flip" not in roles:
        failures.append("no flip beat: the twist re-reads the rule")
    # ponytail: the loop is a shared word stem with the rule; a callback that echoes it only by synonym reads as none
    if not (_words(beats[0]["text"]) - _words(sr["opening"])) & _words(beats[-1]["text"]):
        failures.append("no loop: the last line shares no word with the rule")
    failures += [f"AI-writing lint: {f['rule']}: {f['evidence']!r}" for f in lint(text, story=True)]
    if copied := sorted(_runs(text) & set().union(set(), *map(_runs, examples_of(ws)))):
        failures.append(f"copied from an example story: {copied[0]!r}. Learn their voice, but write your own lines")
    lengths = sorted(len(s.split()) for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s)
    median, long = lengths[len(lengths) // 2], sum(n >= 20 for n in lengths) / len(lengths)
    if median > sr["sentence_median_max"] or long > sr["long_sentence_share_max"]:
        failures.append(f"rhythm: the median sentence runs {median} words and {long:.0%} run 20 or more; the viral "
                        f"stories run 8 and 6%. Keep the median at {sr['sentence_median_max']} or under and long ones "
                        f"under {sr['long_sentence_share_max']:.0%}: split them into short spoken lines")
    if CTA.search(text) or blocked(text, ws.get("narrator_banned", [])) or beats[-1]["text"].rstrip().endswith("?"):
        failures.append("ballot or CTA: no question to the viewer and no ask; the loop is the ending")
    every = " ".join([text, *(str(ep.get(k, "")) for k in ("rule", "situation", "outcome"))])
    failures += [f"canon: {w!r}" for w in blocked(every, ws.get("canon_blocklist", []))]
    failures += [f"blocked word {w!r}" for w in blocked(every, ws["blocked_words"])]
    for c in ep.get("clue_ledger", []):
        p, q = c.get("planted_beat", -1), c.get("pays_off_beat", -1)
        if not (0 <= p < q < len(beats) and beats[q]["role"] == "flip"):
            failures.append(f"unpaid clue {c.get('id')}: planted at beat {p}, paid at beat {q}, not in a flip after it")
    for sit, out, job in history:
        if _same(str(ep.get("situation", "")), sit):
            failures.append(f"repeated situation {sit!r} ({job})")
        if _same(str(ep.get("outcome", "")), out):
            failures.append(f"repeated outcome {out!r} ({job})")
    return failures


def deadlines(sr: dict, total_s: float) -> tuple[float, float]:
    """When the rule and the stakes must have landed: a share of runtime, never under a floor in seconds (a 3% rule is
    about 5 s in a 3-minute reel but 2.1 s in a 70 s Short, under the time "In this world" alone takes; 6 Nov)."""
    return (max(sr["rule_by"] * total_s, sr.get("rule_by_min_s", 0.0)),
            max(sr["stakes_by"] * total_s, sr.get("stakes_by_min_s", 0.0)))


def _prompt(job: Job, ws: dict, fb: list[str], previous: dict | None = None) -> str:
    sr, fmt, rate = story_rules_of(ws), format_of(ws), ws["voice"]["rate"]
    # the narrator's real pace with its pauses when the channel records one (6 Nov: short lines add stops, and 257
    # words at the band's middle ran 73.8 s), else the middle of the voice check's band
    wps = ws["voice"].get("pace_wps") or (rate["lo"] + rate["hi"]) / 2 / (60 if rate["unit"] == "wpm" else 1)
    lo, hi = fmt["length_s"]
    pct = lambda band: f"{band[0]:.0%}-{band[1]:.0%}"  # noqa: E731
    rule_s, stakes_s = deadlines(sr, hi)  # the strictest case: the longest film the band allows
    shape = [f"- {round(lo * wps)}-{round(hi * wps)} words in all, read at about {wps:.1f} words a second.",
             f"- The first beat is the rule, starting \"{sr['opening']}\", finished by about {rule_s:.1f} s: at most "
             f"{max(5, int((rule_s - 0.3) * wps))} words.",
             f"- Stakes landed by about {stakes_s:.1f} s; customs at {pct(sr['customs'])}; a vow at {pct(sr['vow'])}; false hope "
             f"at {pct(sr['false_hope'])}; the threat unseen until {sr['threat_after']:.0%}; then the flip and the "
             "callback.",
             f"- Roles: {', '.join(ROLES)}. Every clue in the ledger is planted earlier and paid off in a flip beat.",
             f"- Rhythm: the median sentence {sr['sentence_median_max']} words or fewer, and under "
             f"{sr['long_sentence_share_max']:.0%} of sentences at 20 words or more."]
    examples = [f"### Example {i}\n{t}" for i, t in enumerate(examples_of(ws), 1)]
    return "\n\n".join([f"Write one {ws['display_name']} {fmt['label']} episode as data from this brief, as your system "
                        "prompt describes.", "## Brief", job.brief.read_text(encoding="utf-8").strip(), "## Shape",
                        "\n".join(shape),
                        *(["## Example stories", "Real reels in this genre that went viral. Learn their voice, rhythm "
                           "and humor. Write your own rule, customs, names, lines and twist, and keep to your system "
                           "prompt's hard lines (our narrator is 16 or older).", *examples] if examples else []),
                        *(["## Your previous draft. Revise it: change only what the list below names, and keep "
                           "everything else.", json.dumps({k: v for k, v in previous.items() if k != "narration"},
                                                          indent=1)] if previous else []),
                        *(["## Fix these from the last draft", "\n".join(f"- {f}" for f in fb)] if fb else [])])


def story(job: Job, *, feedback: list[str] | tuple = (), claude_bin: list[str] | None = None, sensor: Path = SENSOR,
          root: Path = OUT) -> dict:
    """One episode, retried from the shared budget until it passes; a call with feedback (a rewrite) pays from the
    start. Writes episode.json and narration.txt, and records the (situation, outcome) pair."""
    ws, conn = load(job.workspace), store.connect(root)
    history = [(r["situation"], r["outcome"], r["job"]) for r in store.stories(conn, job.workspace) if r["job"] != job.id]
    work, checks = job.dir / "story", job.dir / "checks"
    work.mkdir(exist_ok=True)
    checks.mkdir(exist_ok=True)
    attempts, fb, cost = [], list(feedback), 0.0
    # a rewrite (story_check's, with feedback) revises the episode on disk; a failed draft here is the next one's base
    ep_file = job.dir / "episode.json"
    previous = json.loads(ep_file.read_text(encoding="utf-8")) if fb and ep_file.exists() else None
    while True:
        with job.regeneration(f"story checks still failing after the regeneration budget: {fb[:3]}" if fb else None):
            r = call("story", _prompt(job, ws, fb, previous), cwd=work, schema=SCHEMA,
                     system_file=Path(ws["dir"]) / ws["playbooks"]["story"], claude_bin=claude_bin,
                     usage_log=job.dir / "usage.jsonl", sensor=sensor)
        cost += r.cost_usd or 0.0
        if r.isolation:
            raise RuntimeError(f"story run not isolated, output unused: {r.isolation}")
        if r.data is None:
            raise RuntimeError(f"story returned no valid structured output (exit {r.exit_code}, timed out {r.timed_out})")
        failures = check_episode(r.data, ws, history)
        attempts.append({"failures": failures, "cost_usd": r.cost_usd})
        (checks / "story.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
        if not failures:
            break
        fb, previous = failures, r.data
    ep = {**r.data, "narration": narration_of(r.data)}
    (job.dir / "episode.json").write_text(json.dumps(ep, indent=2), encoding="utf-8")
    (job.dir / "narration.txt").write_text(ep["narration"], encoding="utf-8")
    (job.dir / "script.json").write_text(json.dumps(script_view(ep), indent=2), encoding="utf-8")
    store.record_story(conn, job.workspace, ep["situation"], ep["outcome"], job.id, datetime.now(timezone.utc))
    return {"attempts": len(attempts), "beats": len(ep["beats"]), "cost_usd": round(cost, 4)}
