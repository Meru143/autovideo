"""metadata and compliance lint: Claude writes the title, description and hashtags; code checks them, appends the
workspace's required lines and builds the upload payload from workspace data (design 5.4.7, 5.8, 5.5.2 steps 15-16;
Plan 2b Task 9; FAILURE_MODES.md, metadata). The model never decides privacy, the kids flag or the AI label."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..claude_call import SENSOR, call
from ..jobs import Job
from ..workspace import format_of, instagram_of, load, blocked

SCHEMA = {"type": "object", "required": ["title", "description", "hashtags", "instagram_caption"], "properties": {
    "title": {"type": "string"}, "description": {"type": "string"},
    "hashtags": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}},
    "instagram_caption": {"type": "string"}, "pinned_comment": {"type": "string"}}}
MAX_TITLE, MAX_CAPTION = 70, 2200  # Instagram's caption limit
PROFANITY = re.compile(r"\b(?:fuck\w*|shit\w*|bitch\w*|damn|crap|wtf|asshole\w*|bastard\w*)\b", re.I)
ID_LIKE = re.compile(r"^(?:[\d\W_]+|[0-9a-f]{8,}|untitled.*|video\s*\d*|.*\.(?:mp4|mov|txt))$", re.I)
URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|org|net|io|co|ly|me|app|link|tv)\b", re.I)
BIO_BAIT = re.compile(r"\b(?:link\s+in|check|see|tap)\s+(?:the\s+|my\s+|our\s+)?bio\b", re.I)
YT_TAGS = re.compile(r"\s*#(?:youtube)?shorts\b", re.I)  # YouTube's own tags, dropped from the Reel caption


def _reel_text(m: dict, ws: dict) -> str:
    """The model's caption words, without YouTube's tags or the lines code appends."""
    text = YT_TAGS.sub("", m.get("instagram_caption", ""))
    for line in instagram_of(ws)["required_lines"]:
        text = text.replace(line, "")
    return text.strip()


def reel_caption(m: dict, ws: dict) -> str:
    """The Reel's caption: the model's words, then the channel's Instagram lines once each. Never a source or a credit
    (Plan 3c decision 3; 8.5 items 19 and 21)."""
    return "\n\n".join([_reel_text(m, ws), *instagram_of(ws)["required_lines"]])


def check_metadata(m: dict, ws: dict) -> list[str]:
    failures, title = [], m.get("title", "").strip()
    letters = [c for c in title if c.isalpha()]
    if not title or ID_LIKE.match(title):
        failures.append(f"title is empty or looks like an ID: {title!r}")
    if len(title) > MAX_TITLE:
        failures.append(f"title is {len(title)} characters; keep it to {MAX_TITLE}")
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.5:
        failures.append("title is mostly capitals")
    tags = m.get("hashtags", [])
    if not 1 <= len(tags) <= 3 or not all(re.fullmatch(r"#\w+", t) for t in tags):
        failures.append(f"hashtags must be 1-3 single #words: {tags}")
    text = " ".join([title, m.get("description", ""), *tags, *m.get("tags", [])])
    if PROFANITY.search(text):
        failures.append(f"profanity: {PROFANITY.search(text).group(0)!r}")
    failures += [f"blocked word {w!r}" for w in blocked(text, ws["blocked_words"])]
    body, cap = _reel_text(m, ws), instagram_of(ws)["hashtags_max"]  # the Reel caption (Plan 3c Task 1)
    if not body:
        failures.append("instagram_caption is empty")
    if (n := len(reel_caption(m, ws))) > MAX_CAPTION:
        failures.append(f"instagram_caption is {n} characters with its required lines; Instagram allows 2,200")
    if (n := len(re.findall(r"#\w+", body))) > cap:
        failures.append(f"instagram_caption has {n} hashtags; the cap is {cap}")
    for name, rx in (("has a link", URL), ("has profanity", PROFANITY), ('has "link in bio" bait', BIO_BAIT)):
        if hit := rx.search(body):
            failures.append(f"instagram_caption {name}: {hit.group(0)!r}")
    failures += [f"instagram_caption: blocked word {w!r}" for w in blocked(body, ws["blocked_words"])]
    pin = str(m.get("pinned_comment") or "").strip()
    if pin:
        if len(pin) > 200:
            failures.append(f"pinned_comment is {len(pin)} characters; keep it under 200")
        for name, rx in (("has a link", URL), ("has profanity", PROFANITY)):
            if hit := rx.search(pin):
                failures.append(f"pinned_comment {name}: {hit.group(0)!r}")
        failures += [f"pinned_comment: blocked word {w!r}" for w in blocked(pin, ws["blocked_words"])]
    return failures


def payload(m: dict, ws: dict) -> dict:
    """The upload body: the model's words, its hashtags, the workspace's required lines (once each) and its fixed
    settings. No source and no AI-voice line (operator, 7 Nov; FAILURE_MODES.md, metadata 8)."""
    desc = m["description"].strip()
    for line in ws["required_lines"]:
        desc = desc.replace(line, "").strip()
    for tag in m["hashtags"]:  # the hashtags go on their own line once, even when the model repeats them inline
        desc = re.sub(r"\s*" + re.escape(tag) + r"(?!\w)", "", desc).strip()
    desc = "\n\n".join([desc, " ".join(m["hashtags"]), *ws["required_lines"]])
    return {"snippet": {"title": m["title"].strip(), "description": desc, "tags": m.get("tags", []),
                        "categoryId": ws["category_id"], "defaultLanguage": ws["language"],
                        "defaultAudioLanguage": ws["language"]},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": bool(ws["made_for_kids"]),
                       "containsSyntheticMedia": bool(ws["contains_synthetic_media"])},
            "made_for_kids_rationale": ws["made_for_kids_rationale"]}


def metadata(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    ws = load(job.workspace)
    script = json.loads((job.dir / "script.json").read_text(encoding="utf-8"))
    work = job.dir / "metadata"
    work.mkdir(exist_ok=True)
    attempts, feedback, cost = [], [], 0.0
    while True:
        prompt = "\n\n".join([f"Write the YouTube metadata for this {ws['display_name']} {format_of(ws)['label']}. "
                              "Follow the metadata playbook.",
                              "Also write instagram_caption, the same video's Instagram Reel caption: its hook line, "
                              "one or two plain sentences, then "
                              f"3-{instagram_of(ws)['hashtags_max']} hashtags. No link, URL or \"link in bio\", and "
                              "no #shorts; code adds the required lines.",
                              "Also write pinned_comment: one short (under 100 chars), punchy, opinion-splitting "
                              "first comment tied specifically to the clip moment so the creator can copy-paste and "
                              "pin it right after posting (no hashtags, no links, no generic \"Tell us in the comments\").",
                              "## Narration", script["narration"], "## Hooks", *script.get("hooks", []),
                              *(
                                  [
                                      "## Featured Streamer / Creator",
                                      f"{script.get('creator', '')} ({script.get('creator_handle', '')}) — "
                                      "feature their name in the title/tags where natural and credit their @handle "
                                      "(no URLs/domains) in the description.",
                                  ]
                                  if script.get("creator")
                                  else []
                              ),
                              f"## Blocked words\n{', '.join(ws['blocked_words'])}"]
                             + (["## Your previous draft failed these checks. Fix every one:",
                                 *(f"- {f}" for f in feedback)] if feedback else []))
        with job.regeneration(f"metadata checks still failing after the regeneration budget: {feedback[:3]}"
                              if feedback else None):
            r = call("metadata", prompt, cwd=work, schema=SCHEMA,
                     system_file=Path(ws["dir"]) / ws["playbooks"]["metadata"], claude_bin=claude_bin,
                     usage_log=job.dir / "usage.jsonl", sensor=sensor)
        cost += r.cost_usd or 0.0
        if r.isolation:
            raise RuntimeError(f"metadata run not isolated, output unused: {r.isolation}")
        if r.data is None:
            raise RuntimeError(f"metadata returned no valid structured output (exit {r.exit_code})")
        failures = check_metadata(r.data, ws)
        attempts.append({"failures": failures, "cost_usd": r.cost_usd, "draft": r.data})
        (work / "checks.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
        if not failures:
            break
        feedback = failures  # the next run pays a regeneration
    (job.dir / "metadata.json").write_text(json.dumps(r.data, indent=2), encoding="utf-8")
    (job.dir / "upload_payload.json").write_text(json.dumps(payload(r.data, ws), indent=2), encoding="utf-8")
    (job.dir / "instagram_caption.txt").write_text(reel_caption(r.data, ws), encoding="utf-8")  # the Reel's (Plan 3c)
    return {"attempts": len(attempts), "title": r.data["title"], "cost_usd": round(cost, 4)}
