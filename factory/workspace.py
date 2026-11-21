"""Workspaces are data, not code (Plan 2b Task 1; FAILURE_MODES.md, workspace). Everything an operator tunes lives
in channels/<name>/settings.json plus its playbooks and brand kit, so the future operator frontend only
edits files. load() refuses a broken workspace by name, before any stage runs."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import ROOT

FOLD = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def blocked(text: str, words: list[str]) -> list[str]:
    """The blocked words in text: whole words, any case, typographic quotes folded to plain ones (FAILURE_MODES.md,
    metadata 9). One check for scripts, metadata and topic briefs."""
    text = text.translate(FOLD)
    return [w for w in words if re.search(r"\b" + re.escape(w.translate(FOLD)) + r"\b", text, re.I)]

CHANNELS = ROOT / "channels"
SETTINGS = "settings.json"  # a channel's settings file; BRAND is its art folder
BRAND = "brand"
REQUIRED = ("name", "display_name", "voice", "palette", "fonts", "blocked_words", "narrator_banned", "required_lines",
            "category_id", "language", "made_for_kids", "made_for_kids_rationale",
            "contains_synthetic_media", "youtube_token", "consent", "playbooks", "music")
# scheduled: the tick's own pick, standing per channel (operator, 5 Nov); public: a public channel's picks on YouTube
# and as Reels (Plan 3c; operator, 7 Nov), dated
ACTIONS = {"upload_private", "upload_private_scheduled", "upload_public_scheduled"}
HEX = re.compile(r"#[0-9A-Fa-f]{6}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SLOT = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")  # a posting time, HH:MM in the channel's timezone (Plan 3a)


# A channel's format, with defaults for a channel that leaves a key out (operator, 5 Nov: channels are settings).
FORMAT_DEFAULTS = {"label": "video", "aspect": "9:16", "length_s": [30, 60], "max_hold_s": 3.0, "max_still_s": 1.5,
                   "max_gap_s": 5.0, "pipeline": "explainer"}
ASPECTS = ("9:16", "16:9", "1:1")
PIPELINES = ("explainer", "story", "clip")  # which stage list a channel runs (story format; clip pipeline)
# A story channel's structure rules (design 5.4.2): shares of runtime, except the _s ones in seconds. All [H]: the
# learning loop may tune them. load() refuses unknown keys, wrong types and reversed bands.
STORY_RULES = {"opening": "In this world", "rule_by": 0.03, "stakes_by": 0.08, "enforcer_by_s": 0.5,
               "threat_after": 0.80, "customs": [0.06, 0.18], "vow": [0.20, 0.25], "false_hope": [0.41, 0.48],
               "max_static_s": 2.5, "shot_s": [0.8, 2.5],
               # the viral stories' rhythm (14 reels, 6 Nov): median sentence 8 words, 6% at 20+ words
               "sentence_median_max": 10, "long_sentence_share_max": 0.15,
               # floors under the shares for short films: "In this world" alone takes ~0.8 s (6 Nov)
               "rule_by_min_s": 3.0, "stakes_by_min_s": 6.5}
# A channel's script rules, with defaults (operator, 5 Nov); load() refuses unknown keys and other types.
SCRIPT_RULES = {"hook_max_words": 10, "rehook_gap_s": 8, "min_facts": 1, "min_quoted_lines": 2, "story_checks": True}
# Reels (Plan 3c): off until switched on; 3 to hashtags_max hashtags (decision 5); no required lines unless the
# channel names them (operator, 7 Nov: no AI-voice line in captions; Meta's AI tag says it instead).
INSTAGRAM_DEFAULTS = {"enabled": False, "hashtags_max": 5}


def channels_root() -> Path:
    """The channels folder: FACTORY_WORKSPACES at call time (tests, tools), else channels/."""
    return Path(os.environ.get("FACTORY_WORKSPACES") or CHANNELS)


def channels() -> list[str]:
    """Every channel folder with a settings.json; a folder starting with "_" is a template, never a channel."""
    return sorted(p.parent.name for p in channels_root().glob(f"*/{SETTINGS}") if not p.parent.name.startswith("_"))


def format_of(ws: dict) -> dict:
    """The channel's format: label (what the prompts call a video), aspect, length band and closing hold."""
    return {**FORMAT_DEFAULTS, **ws.get("format", {})}


def instagram_of(ws: dict) -> dict:
    """A channel's Reels settings, with defaults for any key it leaves out."""
    return {**INSTAGRAM_DEFAULTS, "required_lines": [], **ws.get("instagram", {})}


def story_rules_of(ws: dict) -> dict:
    """A story channel's structure rules, with STORY_RULES filling any key it leaves out."""
    return {**STORY_RULES, **ws.get("story_rules", {})}


def _phrasing_ok(ph) -> bool:
    """A cloned voice's phrasing (FAILURE_MODES.md, clone phrasing): pauses by kind, talking speed, and intensity
    ([exaggeration, cfg]) by story role, with a default for any other role."""
    def num(x, lo, hi):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and lo <= x <= hi

    def pair(p):
        return isinstance(p, list) and len(p) == 2 and num(p[0], 0.25, 0.8) and num(p[1], 0.2, 0.8)
    if not (isinstance(ph, dict) and isinstance(ph.get("pauses"), dict) and isinstance(ph.get("roles"), dict)):
        return False
    return (all(num(ph["pauses"].get(k), 0, 2) for k in ("sentence", "beat", "twist", "last"))
            and num(ph.get("speed"), 1.0, 1.35) and pair(ph.get("default")) and all(map(pair, ph["roles"].values())))


def load(name: str, *, need: tuple[str, ...] = REQUIRED, root: Path | None = None,
         playbook_root: Path | None = None) -> dict:
    path = (root or channels_root()) / name / SETTINGS
    try:
        w = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None
    base = playbook_root or path.parent

    def bad(why: str):
        raise ValueError(f"{path}: {why}")
    if missing := [k for k in need if k not in w]:
        bad(f"missing {', '.join(missing)}")
    if "voice" in need:
        v, r = w["voice"], w["voice"].get("rate", {})
        if v.get("engine") not in ("gemini", "clone"):
            bad("voice.engine must be gemini or clone")
        if r.get("unit") not in ("wpm", "wps") or not 0 < r.get("lo", 0) < r.get("hi", 0):
            bad("voice.rate needs unit wpm or wps and 0 < lo < hi")
        if v["engine"] == "gemini" and not (v.get("voice") and v.get("style")):
            bad("a gemini voice needs voice and style")
        if v["engine"] == "clone" and not all(v.get(k) for k in ("prompt", "real_vocals", "tune")):
            bad("a cloned voice needs prompt, real_vocals and tune (paths from the repo root)")
        if "pace_wps" in v and not (type(v["pace_wps"]) in (int, float) and 1 <= v["pace_wps"] <= 6):
            bad("voice.pace_wps must be 1-6 words a second: the narrator's measured pace, pauses included")
        if v["engine"] == "clone" and not _phrasing_ok(v.get("phrasing")):
            bad("voice.phrasing needs pauses (sentence, beat, twist, last: 0-2 s), speed 1.0-1.35, and default and "
                "roles as [exaggeration 0.25-0.8, cfg 0.2-0.8]; exaggeration over 0.8 whispered and slurred (6 Nov)")
    if "palette" in need and (wrong := [k for k, c in w["palette"].items() if not HEX.fullmatch(str(c))]):
        bad(f"palette colours must be #RRGGBB: {wrong}")
    if "blocked_words" in need and not w["blocked_words"]:
        bad("blocked_words is empty")
    if "required_lines" in need and not (isinstance(w["required_lines"], list)
                                         and all(str(x).strip() for x in w["required_lines"])):
        bad("required_lines must be a list of non-empty lines; [] for none")
    if "consent" in need:
        for c in w["consent"]:
            if c.get("action") not in ACTIONS or not DATE.fullmatch(str(c.get("granted", ""))):
                bad(f"each consent entry needs an action in {sorted(ACTIONS)} and an ISO date: {c}")
    if "format" in w:  # a channel's format (operator, 5 Nov); FORMAT_DEFAULTS fill what it leaves out
        fm, length = w["format"], w["format"].get("length_s", FORMAT_DEFAULTS["length_s"])
        if fm.get("aspect", "9:16") not in ASPECTS:
            bad(f"format.aspect must be one of {ASPECTS}")
        if fm.get("pipeline", "explainer") not in PIPELINES:
            bad(f"format.pipeline {fm.get('pipeline')!r} must be one of {PIPELINES}")
        if not (isinstance(length, list) and len(length) == 2 and all(type(x) in (int, float) for x in length)
                and 0 < length[0] < length[1]):
            bad("format.length_s must be [lo, hi] seconds with 0 < lo < hi")
        if extra := sorted(set(fm) - set(FORMAT_DEFAULTS)):  # a typo never falls back to a default silently
            bad(f"format has unknown keys {extra}; it takes {sorted(FORMAT_DEFAULTS)}")
        hold = fm.get("max_hold_s", FORMAT_DEFAULTS["max_hold_s"])
        if type(hold) not in (int, float) or not 0 <= hold <= 10 or not isinstance(fm.get("label", ""), str):
            bad("format.max_hold_s must be 0-10 seconds and format.label text")
        for k, top in (("max_still_s", 10), ("max_gap_s", 20)):  # the pacing limits (engine Task 1)
            v = fm.get(k, FORMAT_DEFAULTS[k])
            if type(v) not in (int, float) or not 0 < v <= top:
                bad(f"format.{k}={v!r} must be seconds above 0, at most {top}")
    if format_of(w)["pipeline"] == "story":  # FAILURE_MODES.md, workspace 10-14
        if "story_rules" not in w:
            bad("a story channel (format.pipeline story) needs story_rules")
        if extra := sorted(set(w["story_rules"]) - set(STORY_RULES)):
            bad(f"story_rules has unknown keys {extra}; it takes {sorted(STORY_RULES)}")
        sr, num = story_rules_of(w), (int, float)
        if not (isinstance(sr["opening"], str) and sr["opening"].strip()):
            bad("story_rules.opening is empty: every story opens with it")
        for k in ("rule_by", "stakes_by", "threat_after"):
            if type(sr[k]) not in num or not 0 < sr[k] <= 1:
                bad(f"story_rules.{k}={sr[k]!r} must be a share of runtime above 0, at most 1")
        for k in ("enforcer_by_s", "max_static_s"):
            if type(sr[k]) not in num or sr[k] <= 0:
                bad(f"story_rules.{k}={sr[k]!r} must be seconds above 0")
        for k in ("customs", "vow", "false_hope", "shot_s"):
            v = sr[k]
            if not (isinstance(v, list) and len(v) == 2 and all(type(x) in num for x in v) and 0 <= v[0] < v[1]
                    and (k == "shot_s" or v[1] <= 1)):
                bad(f"story_rules.{k}={v!r} must be [lo, hi] with lo < hi" + ("" if k == "shot_s" else ", shares 0-1"))
        canon = w.get("canon_blocklist", [])
        if not (isinstance(canon, list) and all(isinstance(x, str) and x.strip() for x in canon)):
            bad("canon_blocklist must list the famous works a premise may not retell")
    for k, v in w.get("script_rules", {}).items():  # "false" is truthy; 12 under a misspelt key is ignored
        want = type(SCRIPT_RULES[k]) if k in SCRIPT_RULES else None
        if want is None or not (type(v) is want or (want is int and type(v) is float)):
            bad(f"script_rules.{k}={v!r}: the keys are {sorted(SCRIPT_RULES)}, story_checks true or false, "
                "the rest numbers")
    if "instagram" in w:  # Reels (Plan 3c; FAILURE_MODES.md, workspace 15): "false" is truthy, so never read as on
        ig = w["instagram"] if isinstance(w["instagram"], dict) else bad("instagram must be an object")
        lines = ig.get("required_lines", ["x"])
        if extra := sorted(set(ig) - {*INSTAGRAM_DEFAULTS, "required_lines"}):
            bad(f"instagram has unknown keys {extra}; it takes enabled, hashtags_max and required_lines")
        if type(ig.get("enabled", False)) is not bool:
            bad("instagram.enabled must be true or false")
        if not (type(ig.get("hashtags_max", 5)) is int and 3 <= ig.get("hashtags_max", 5) <= 30):
            bad("instagram.hashtags_max must be a whole number from 3 to 30 (Instagram allows 30)")
        if not (isinstance(lines, list) and lines and all(isinstance(x, str) and x.strip() for x in lines)):
            bad("instagram.required_lines must be non-empty lines")
    if "schedule" in w:  # posting slots (Plan 3a); optional, so a production-only channel still loads
        sc = w["schedule"]
        if not (isinstance(sc.get("slots"), list) and sc["slots"] and all(SLOT.fullmatch(str(x)) for x in sc["slots"])):
            bad("schedule.slots must list HH:MM times between 00:00 and 23:59")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(sc.get("timezone", "")))
        except Exception:
            bad(f"schedule.timezone {sc.get('timezone')!r} isn't an IANA timezone such as America/New_York")
        if "max_jobs" in sc and not (type(sc["max_jobs"]) is int and sc["max_jobs"] >= 0):
            bad("schedule.max_jobs must be a whole number of videos, 0 or more (leave it out for no cap)")
        if "max_per_day" in sc and not (type(sc["max_per_day"]) is int and sc["max_per_day"] >= 1):
            bad("schedule.max_per_day must be a whole number of videos a day, 1 or more (leave it out for no cap)")
        if "cap_day" in sc and not (type(sc["cap_day"]) is int and 1 <= sc["cap_day"] <= 24):
            bad("schedule.cap_day must be a whole number from 1 to 24")
        if "cap_24h" in sc and not (type(sc["cap_24h"]) is int and 1 <= sc["cap_24h"] <= 48):
            bad("schedule.cap_24h must be a whole number from 1 to 48")
        if sc.get("mode", "private") not in ("private", "public"):  # a typo is refused, never read as either
            bad(f'schedule.mode {sc["mode"]!r} must be "private" (the default) or "public"')
    if "topics" in w:  # the topic engine's settings (Plan 3a)
        tp = w["topics"]
        if not (type(tp.get("min_score", 60)) in (int, float) and 0 < tp.get("min_score", 60) <= 100):
            bad("topics.min_score must be between 1 and 100")
        if not (isinstance(tp.get("levers"), list) and tp["levers"]
                and all(isinstance(x, str) and x.strip() for x in tp["levers"])):
            bad("topics.levers must be a non-empty list of lever names")
        if not (type(tp.get("ideas_per_pass", 6)) is int and 1 <= tp.get("ideas_per_pass", 6) <= 12):
            bad("topics.ideas_per_pass must be a whole number from 1 to 12")
    if "music" in w:  # the music writer's palette (Plan 2c); optional until a workspace has one
        m, bpm = w["music"], w["music"].get("bpm") or [0, 0]
        if not (len(bpm) == 2 and 60 <= bpm[0] < bpm[1] <= 140):
            bad("music.bpm must be [lo, hi] with 60 <= lo < hi <= 140")
        if not (isinstance(m.get("modes"), list) and m["modes"]):
            bad("music.modes must list the allowed modes")
        if not (m.get("palette") and type(m.get("version")) is int):
            bad("music needs a palette name and an integer version")
        if type(m.get("swell_db", 0)) not in (int, float) or not 0 <= m.get("swell_db", 0) <= 6:
            bad("music.swell_db must be 0-6 dB (the mixer's rise in the gaps)")
    if "playbooks" in need and "music" not in w["playbooks"]:
        bad("playbooks.music is missing: every channel's music writer reads it")
    for key in ("playbooks", "fonts"):
        for role, rel in (w[key].items() if key in need else ()):
            f = base / rel
            if not f.is_file() or (key == "playbooks" and not f.read_text(encoding="utf-8").strip()):
                bad(f"{key[:-1]} for {role} is missing or empty: {rel}")
    w["dir"] = str(base)
    return w
