"""script and script_checks: Claude writes the narration from the saved source copies; code checks every claim, the
narrator persona, the brand's words and the length (design 5.4.2, 5.5.2 steps 5-6, 6d(b); Plan 2b Task 5;
FAILURE_MODES.md, script). A failed check goes back to the writer with the failure list, spending one regeneration."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from ..claude_call import SENSOR, call
from ..jobs import Job
from ..script_lint import lint
from ..workspace import SCRIPT_RULES, format_of, load, blocked

SCHEMA = {"type": "object", "required": ["hooks", "beats", "narration", "send_trigger", "added_value", "takeaway"],
          "properties": {
              "hooks": {"type": "array", "items": {"type": "string"}},
              "beats": {"type": "array", "items": {"type": "object", "required": ["text", "claims"], "properties": {
                  "text": {"type": "string"},
                  "claims": {"type": "array", "items": {"type": "object", "required": ["source_id", "quote"],
                                                        "properties": {"source_id": {"type": "string"},
                                                                       "quote": {"type": "string"}}}}}}},
              "narration": {"type": "string"}, "send_trigger": {"type": "string"}, "added_value": {"type": "string"},
              "takeaway": {"type": "string"}}}
CRITIC = {"type": "object", "required": ["learns", "matches", "fact_agrees", "needs_explaining", "notes"],
          "properties": {"learns": {"type": "string"}, "matches": {"type": "boolean"},
                         "fact_agrees": {"type": "boolean"},
                         "needs_explaining": {"type": "array", "items": {"type": "string"}},
                         "notes": {"type": "string"}}}
TAKEAWAY_MAX_WORDS = 15
MAX_SOURCE_CHARS = 40000
PAUSE_S = 0.5
QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
RULES = SCRIPT_RULES  # defaults; a channel's script_rules override any of them (operator, 5 Nov)
NUMBER = (r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty|hundred"
          r"|thousand|million|half|quarter)\b")
TURN = r"^(?:but|so|here's|now|then|instead|turns out|that's why|which means|the fix|try)\b"  # list words aren't turns
# Story-first (operator, 5 Nov; teardowns of ThatGuy, Improvement Pill, Oops Productive): one story, no list, no lecture.
# Noun context only (Plan 2c final review I8): "the tip of your tongue", "you study their face", "here are two
# coworkers" and "your number one fear" are story lines, not lists or lectures.
LIST = re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|top|pro|quick|simple|easy|best|these|those|my|some)\s+"
                  r"tips?\b|\bstep (?:one|two|three|four|five|\d)\b|(?:^|(?<=[.!?:]\s))number (?:one|two|three|\d)\b"
                  r"|\bnumber (?:one|two|three|\d)\s*[:,]|\bhere are (?:\d+|two|three|four|five) (?:ways|tips|things|reasons"
                  r"|signs|steps|mistakes|rules|tricks|habits|phrases|lines|questions)\b|\b(?:firstly|secondly|thirdly)\b",
                  re.IGNORECASE)
STUDY = re.compile(r"\bresearch(?:ers?)?\b|\bscientists?\b|\b(?:a|the|one|this|that|new|recent|famous|landmark|\d{4})\s+"
                   r"stud(?:y|ies)\b|\bstudies (?:show|showed|found|suggest|say)\b|\bstudy (?:found|shows|showed|"
                   r"suggests|says)\b|\bexperiments?\b|\bdatasets?\b", re.IGNORECASE)


def _rehook(sentence: str) -> bool:
    """A re-hook: a new number, a question, or a turn ("But", "So", "Here's", "The fix")."""
    return bool(re.search(NUMBER, sentence, re.I) or "?" in sentence or re.search(TURN, sentence, re.I))


def _norm(text: str) -> str:
    """Only whitespace, curly quotes, dashes and case are normalised: a changed word or number still differs."""
    t = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(re.sub(r"[‐‑‒–—]", "-", t).split())


def check_script(data: dict, sources: dict[str, tuple[str, str]], ws: dict) -> tuple[list[str], dict]:
    """(failures, stats) for one draft. sources maps id -> (group, saved text)."""
    failures, narration = [], data.get("narration", "")
    rules, (lo, hi) = {**RULES, **ws.get("script_rules", {})}, format_of(ws)["length_s"]
    if not 3 <= len(data.get("hooks", [])) <= 5:
        failures.append(f"{len(data.get('hooks', []))} hooks; write 3-5")
    claims = [c for b in data.get("beats", []) for c in b.get("claims", [])]
    for c in claims:
        if c["source_id"] not in sources:
            failures.append(f"claim cites unknown source {c['source_id']!r}")
        elif _norm(c["quote"]) not in _norm(sources[c["source_id"]][1]):
            failures.append(f"quote not found in {c['source_id']}: {c['quote'][:80]!r}")
    groups = {" ".join(sources[c["source_id"]][0].lower().split()) for c in claims if c["source_id"] in sources}
    if len(claims) < rules["min_facts"]:  # a channel setting; one is enough (operator, 5 Nov; design 8.5 item 13)
        failures.append(f"{'no' if not claims else 'too few'} sourced fact{'s' if claims else ''}: make at least "
                        f"{rules['min_facts']}, each quoted verbatim from a source snapshot")
    tags = re.findall(r"<[^>]*>", narration)
    if bad := sorted({t for t in tags if t != "<short pause>"}):
        failures.append(f"tags other than <short pause> would be read aloud: {bad}")
    spoken = re.sub(r"<[^>]*>", " ", narration)
    words = len(re.findall(r"[A-Za-z0-9']+", spoken))
    wpm = ws["voice"].get("measured_wpm") or (ws["voice"]["rate"]["lo"] + ws["voice"]["rate"]["hi"]) / 2
    est = words / (wpm / 60) + PAUSE_S * tags.count("<short pause>")
    if not lo <= est <= hi:
        failures.append(f"estimated {est:.0f} s at {wpm:.0f} wpm; write {lo}-{hi} s")
    narrator = QUOTED.sub(" ", spoken)  # a practised line the viewer says, in quotes, may use "I"
    for banned in ws["narrator_banned"]:
        flags = 0 if banned in ("I", "Dr.") else re.IGNORECASE
        if re.search(r"(?<![\w'])" + re.escape(banned) + (r"(?![\w'])" if banned[-1].isalnum() else ""), narrator, flags):
            failures.append(f"the narrator says {banned!r} (never an I, a coach, a psychologist or a Dr.)")
    if rules["story_checks"] and (m := LIST.search(narrator)):
        failures.append(f"list scaffolding in the narration ({m.group(0)!r}): tell one story, with no tips, steps or "
                        "numbering")
    if rules["story_checks"] and (m := STUDY.search(narrator)):
        failures.append(f"study talk in the narration ({m.group(0)!r}): the fact lands as a reveal, and its verbatim "
                        "quote lives in the claim")
    if (quoted := len(QUOTED.findall(spoken))) < rules["min_quoted_lines"]:
        failures.append(f"{quoted} quoted line(s); quote at least {rules['min_quoted_lines']}: the line that goes "
                        "wrong and the line to say")
    everything =" ".join([spoken, *data.get("hooks", []), *(b.get("text", "") for b in data.get("beats", []))])
    failures += [f"blocked word {w!r}" for w in blocked(everything, ws["blocked_words"])]
    flat, sign = " ".join(spoken.split()), " ".join(ws.get("sign_off", "").split())  # optional (operator, 5 Nov)
    if sign and not flat.endswith(sign):
        failures.append(f"the last line must be the sign-off: {ws['sign_off']!r}")
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", flat) if s]
    hook = sentences[0] if sentences else ""
    hook_words = len(re.findall(r"[A-Za-z0-9']+", hook))
    if hook_words > rules["hook_max_words"]:  # 5.4.2: complete by about 2 s; a question or a scene (8.5 item 13)
        failures.append(f"the hook runs {hook_words} words (~{hook_words / (wpm / 60):.1f} s); "
                        f"keep it to {rules['hook_max_words']}")
    body = [s for s in re.split(r"(?<=[.?!])\s+", flat[:len(flat) - len(sign)] if flat.endswith(sign) else flat) if s]
    gap = worst = 0
    start = span = None
    for i, s in enumerate(body[1:-1], 1):  # after the hook, before the CTA that leads into the sign-off
        if _rehook(s):
            gap, start = 0, None
            continue
        start = i if start is None else start
        gap += len(re.findall(r"[A-Za-z0-9']+", s))
        if gap > worst:
            worst, span = gap, (start, i)
    if worst > rules["rehook_gap_s"] * wpm / 60:  # 8.5 item 4: re-hooks every 5-8 s
        a, b = (body[span[0]].split(), body[span[1]].split())
        where = f"from \"{' '.join(a[:6])}...\" to \"...{' '.join(b[-6:])}\""  # a rewrite can't fix what it can't find
        failures.append(f"a stretch of {worst} words (~{worst / (wpm / 60):.0f} s) has no re-hook, {where}. Put a number, "
                        f"a question or a turn (But, So, Here's, Then, Instead) inside it; keep gaps under "
                        f"{rules['rehook_gap_s']} s")
    failures += [f"AI-writing lint: {f['rule']}: {f['evidence']!r}" for f in lint(spoken)]
    takeaway = (data.get("takeaway") or "").split()
    if not takeaway or len(takeaway) > TAKEAWAY_MAX_WORDS:
        failures.append(f"state the takeaway as one plain sentence of {TAKEAWAY_MAX_WORDS} words or fewer that a "
                        f"viewer could repeat (yours has {len(takeaway)})")
    return failures, {"words": words, "est_seconds": round(est, 1), "claims": len(claims), "groups": len(groups),
                      "hook_words": hook_words, "worst_rehook_gap_words": worst}


def _prompt(brief: str, sources: dict[str, tuple[str, str]], urls: dict[str, str], ws: dict, feedback: list[str],
            earlier: list[str] = (), previous: dict | None = None) -> str:
    fm = format_of(ws)
    parts = [f"Write the narration for this {ws['display_name']} {fm['label']}. Follow the script playbook in your "
             "system prompt.",
             "## Brief", brief.strip(), "## Sources: the only facts you may use. Every claim quotes one verbatim, by ID."]
    for sid, (group, text) in sources.items():
        cut = text[:MAX_SOURCE_CHARS] + ("\n[truncated]" if len(text) > MAX_SOURCE_CHARS else "")
        parts += [f"### {sid} ({group}) {urls.get(sid, '')}", cut]
    wpm = ws["voice"].get("measured_wpm") or (ws["voice"]["rate"]["lo"] + ws["voice"]["rate"]["hi"]) / 2
    parts += ["## Workspace rules",
              f"- Blocked words: {', '.join(ws['blocked_words'])}.",
              f"- The narrator never says: {', '.join(ws['narrator_banned'])}. A line the viewer will say, in quotes, "
              "may use \"I\".",
              *([f"- The last line, exactly: \"{ws['sign_off']}\""] if ws.get("sign_off") else
                ["- End on the send trigger. There is no sign-off line."]),
              "- The only tag allowed in the narration is <short pause>.",
              f"- Length: {fm['length_s'][0]}-{fm['length_s'][1]} s at about {wpm:.0f} words a minute."]
    if previous is not None:  # revise, never restart: a blank-page rewrite brings new problems (6 Nov, FAILURE_MODES.md)
        parts += ["## Your previous draft. Revise it: change only what the checks below name, and keep everything else.",
                  json.dumps(previous, indent=1)]
    if feedback:
        parts += ["## Your previous draft failed these checks. Fix every one:", *(f"- {f}" for f in feedback)]
    if earlier:  # a fix that undoes an earlier fix spends the budget twice (6 Nov: three drafts, three failures)
        parts += ["## Earlier drafts also failed these. Keep them fixed:", *(f"- {f}" for f in earlier)]
    return "\n\n".join(parts)


def message_check(data: dict, ws: dict, job: Job, claude_bin, sensor) -> tuple[list[str], float]:
    """One cheap no-tools read of the script as a viewer would (FAILURE_MODES.md, script 14-15): what a viewer
    learns against the stated takeaway, the fact against the story, and anything needing its own explanation.
    A malformed or unisolated reply fails open; the final review still judges the video."""
    facts = [c["quote"] for b in data.get("beats", []) for c in b.get("claims", [])]
    prompt = (f"You are a viewer who knows nothing about this topic, watching a {ws['display_name']} short video. "
              "Read only its narration; lines in double quotes are spoken by the characters.\n\n"
              f"## Narration\n{data['narration']}\n\n## The takeaway the writer intends\n{data['takeaway']}\n\n"
              "## The fact it rests on\n" + "\n".join(f"- {q}" for q in facts) + "\n\n"
              "Answer as the schema asks. learns: in one plain sentence, what a viewer learns. matches: whether "
              "that is the intended takeaway. fact_agrees: whether the fact agrees with what the story shows "
              "(false if it argues against the scene). needs_explaining: anything in the story that needs its own "
              "explanation and doesn't serve the takeaway, such as a second gimmick or a side joke. notes: what to "
              "change, specifically.")
    with tempfile.TemporaryDirectory() as td:
        r = call("critic", prompt, cwd=Path(td), schema=CRITIC, claude_bin=claude_bin,
                 usage_log=job.dir / "usage.jsonl", sensor=sensor)
    v = r.data
    if r.isolation or not isinstance(v, dict) or not all(k in v for k in CRITIC["required"]):
        return [], [], r.cost_usd or 0.0  # fail open
    # The message failing to land, or the fact arguing with the story, blocks: those sank story-test C. "Needs
    # explaining" alone is advisory. A strict reader always finds one more term to set up, and it quarantined two
    # technical scripts whose message landed (6 Nov). It rides along when the draft fails for a real reason, and the
    # final review still judges the video.
    extra = f" Also cut what needs its own explanation: {'; '.join(v['needs_explaining'])}" if v["needs_explaining"] else ""
    fails = []
    if not v["matches"]:
        fails.append(f"message check: a viewer would take away \"{v['learns']}\", not your takeaway "
                     f"\"{data['takeaway']}\". {v['notes']}{extra}")
    if not v["fact_agrees"]:
        fails.append(f"message check: the fact argues against the story. {v['notes']}{extra if v['matches'] else ''}")
    advisories = [] if fails else [f"message check (advisory): {x}" for x in v["needs_explaining"]]
    return fails, advisories, r.cost_usd or 0.0


def script(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    ws = load(job.workspace)
    index = json.loads((job.dir / "sources" / "index.json").read_text(encoding="utf-8"))["sources"]
    sources = {s["id"]: (s["group"], (job.dir / "sources" / f"{s['id']}.txt").read_text(encoding="utf-8")) for s in index}
    urls = {s["id"]: s.get("url", "") for s in index}
    work, checks_dir = job.dir / "script", job.dir / "checks"
    work.mkdir(exist_ok=True)
    checks_dir.mkdir(exist_ok=True)
    attempts, feedback, earlier, previous, cost = [], [], [], None, 0.0
    while True:
        with job.regeneration(f"script checks still failing after the regeneration budget: {feedback[:3]}"
                              if feedback else None):
            r = call("script", _prompt(job.brief.read_text(encoding="utf-8"), sources, urls, ws, feedback, earlier,
                                       previous), cwd=work,
                     schema=SCHEMA, system_file=Path(ws["dir"]) / ws["playbooks"]["script"], claude_bin=claude_bin,
                     usage_log=job.dir / "usage.jsonl", sensor=sensor)
        cost += r.cost_usd or 0.0
        if r.isolation:
            raise RuntimeError(f"script run not isolated, output unused: {r.isolation}")
        if r.data is None:
            raise RuntimeError(f"script returned no valid structured output (exit {r.exit_code}, timed out {r.timed_out})")
        failures, stats = check_script(r.data, sources, ws)
        advisories = []
        if not failures:  # the cheap message check runs only on a draft the code checks passed
            failures, advisories, spent = message_check(r.data, ws, job, claude_bin, sensor)
            cost += spent
        attempts.append({"failures": failures, "advisories": advisories, **stats, "cost_usd": r.cost_usd})
        (checks_dir / "script.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
        if not failures:
            break
        earlier += [f for f in feedback if f not in failures and f not in earlier]
        feedback, previous = failures, r.data  # the next run revises this draft and pays a regeneration
    (job.dir / "script.json").write_text(json.dumps(r.data, indent=2), encoding="utf-8")
    (job.dir / "narration.txt").write_text(r.data["narration"], encoding="utf-8")
    return {"attempts": len(attempts), **stats, "cost_usd": round(cost, 4)}
