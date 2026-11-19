"""The factory's own settings as data (operator, 10 Nov: "theres no settings page in console where i can control general
settings of the factory"): factory/settings.json, or FACTORY_SETTINGS at call time (tests, tools). Read at each use, so
an edit applies on the next Claude call or tick with no restart. check() refuses a file the factory can't run on,
naming the field; the console checks every edit with it before writing.

- paused: the tick serves no channel (a post already uploading still finishes).
- videos_at_once: at most this many jobs run at once; the third still needs scheduler.ROOM_GB of memory.
- auto_retries: how often the tick retries a failed or blocked video by itself before anyone looks.
- stop_at: the factory waits at this much of the plan's week or 5-hour window (no 5-hour stop below full, operator
  8 Nov); model_weekly is for a model's own weekly bar (Fable on the Pro plan, 10 Nov): from there, a step on that
  model runs on the fallback model and effort instead of waiting.
- steps: each Claude step's model and effort. Why each level is what it is: git log -- factory/settings.json and the
  claude_call.py history before it (operator, 10 Nov: "decrease efforts for all"; the Fable 5.2 trial)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import ROOT

STEPS = ("research", "topics", "judge", "critic", "script", "story", "shots", "cast", "sets", "scene", "music", "fix",
         "repair", "review", "metadata", "lessons", "loop")
MODELS = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-5-5": "Haiku 5.5",
          "claude-fable-5-2": "Fable 5.2"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
COUNTS = {"videos_at_once": (1, 3), "auto_retries": (0, 3)}
STOPS = ("weekly", "five_hour", "model_weekly")
KEYS = ("paused", "videos_at_once", "auto_retries", "stop_at", "fallback", "steps")


def path() -> Path:
    return Path(os.environ.get("FACTORY_SETTINGS") or ROOT / "factory" / "settings.json")


def load(p: Path | None = None) -> dict:
    """The checked settings; ValueError, in plain words, when the file is missing, isn't JSON or fails check()."""
    p = p or path()
    try:
        s = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{p.name} doesn't load: {e}") from None
    check(s)
    return s


def _exactly(d, keys, where: str) -> None:
    if not isinstance(d, dict) or set(d) != set(keys):
        got = set(d) if isinstance(d, dict) else set()
        raise ValueError(f"{where} takes exactly {', '.join(keys)}: missing {sorted(set(keys) - got)}, "
                         f"unknown {sorted(got - set(keys))}")


def _choice(c, where: str) -> None:
    _exactly(c, ("model", "effort"), where)
    if c["model"] not in MODELS:
        raise ValueError(f"{where}.model must be one of {', '.join(MODELS)}")
    if c["effort"] not in EFFORTS:
        raise ValueError(f"{where}.effort must be one of {', '.join(EFFORTS)}")


def check(s) -> None:
    _exactly(s, KEYS, "the settings file")
    if type(s["paused"]) is not bool:
        raise ValueError("paused must be true or false")
    for k, (lo, hi) in COUNTS.items():
        if type(s[k]) is not int or not lo <= s[k] <= hi:
            raise ValueError(f"{k} must be a whole number from {lo} to {hi}")
    _exactly(s["stop_at"], STOPS, "stop_at")
    for k in STOPS:
        if type(s["stop_at"][k]) not in (int, float) or not 0 < s["stop_at"][k] <= 100:
            raise ValueError(f"stop_at.{k} must be a percentage above 0, up to 100")
    _choice(s["fallback"], "fallback")
    _exactly(s["steps"], STEPS, "steps")
    for k in STEPS:
        _choice(s["steps"][k], f"steps.{k}")
