"""story_check: the rule and the stakes land on time, measured on the narrated timing (story plan Task 2;
FAILURE_MODES.md, story 1-2). A late story is rewritten from the shared budget and narrated again. The voice's own
checks already hold the speaking rate to the channel's band, so this stage doesn't repeat them."""
from __future__ import annotations

import json

from ..jobs import Job
from ..workspace import load, story_rules_of


def beat_times(ep: dict, timing: dict) -> list[tuple[float, float]]:
    """(start, end) seconds per beat: the beats' word counts laid onto the timed words in proportion."""
    # ponytail: proportional mapping; ASR can split or merge a word, so a beat edge may drift by a word or two
    counts, words = [len(b["text"].split()) for b in ep["beats"]], timing["words"]
    scale, out, cum = len(words) / max(sum(counts), 1), [], 0
    for n in counts:
        a = min(int(cum * scale), len(words) - 1)
        cum += n
        b = min(max(round(cum * scale) - 1, a), len(words) - 1)
        out.append((words[a]["start"], words[b]["end"]))
    return out


def check_timing(ep: dict, timing: dict, ws: dict) -> list[str]:
    from .story import deadlines
    sr, total = story_rules_of(ws), timing["audio_duration_s"]
    rule_s, stakes_s = deadlines(sr, total)
    times, roles, failures = beat_times(ep, timing), [b["role"] for b in ep["beats"]], []
    if times[0][1] > rule_s:
        failures.append(f"late rule: it ends at {times[0][1]:.1f} s; it must end by {rule_s:.1f} s "
                        f"({sr['rule_by']:.0%} of {total:.0f} s, at least {sr.get('rule_by_min_s', 0):g} s): shorten it")
    if "stakes" not in roles:
        failures.append("no stakes beat")
    elif (end := times[roles.index("stakes")][1]) > stakes_s:
        failures.append(f"late stakes: they end at {end:.1f} s; they must land by {stakes_s:.1f} s "
                        f"({sr['stakes_by']:.0%}, at least {sr.get('stakes_by_min_s', 0):g} s)")
    return failures


def story_check(job: Job, *, rewrite=None, renarrate=None) -> dict:
    """Loops until the timing passes: each failure rewrites the story (a paid regeneration, Quarantined when the
    budget is spent) and narrates it again. rewrite(job, feedback) and renarrate(job) stand in for tests."""
    from .narrate import narrate
    from .story import story
    ws, rounds = load(job.workspace), []
    (job.dir / "checks").mkdir(exist_ok=True)
    while True:
        ep = json.loads((job.dir / "episode.json").read_text(encoding="utf-8"))
        timing = json.loads((job.dir / "timing.json").read_text(encoding="utf-8"))
        failures = check_timing(ep, timing, ws)
        rounds.append({"failures": failures, "seconds": timing["audio_duration_s"]})
        (job.dir / "checks" / "story_timing.json").write_text(json.dumps(rounds, indent=2), encoding="utf-8")
        if not failures:
            return {"rounds": len(rounds), "seconds": timing["audio_duration_s"]}
        (rewrite or (lambda j, fb: story(j, feedback=fb)))(job, failures)
        (renarrate or narrate)(job)
