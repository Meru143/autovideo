"""gate_review and the repair round: one block-only Claude review per video reads the contact sheet, the script and
the metadata; a block with fix notes gets one repair round from the shared regeneration budget, its gates, then the
review runs again (design 5.7 check 45, R6 Q42, R8 Q54, 8.5 item 5; Plan 2b Task 10; FAILURE_MODES.md, review). An
interrupted repair resumes from repair_pending.json, never with a fresh verdict on whatever is on disk."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from ..claude_call import SENSOR, QuotaWait, call
from ..jobs import Job, Quarantined
from ..render import LEFT_FRAMES
from ..workspace import format_of, load

SCHEMA = {"type": "object", "required": ["block", "reasons", "defects", "fix_notes"], "properties": {
    "block": {"type": "boolean"}, "reasons": {"type": "array", "items": {"type": "string"}},
    "defects": {"type": "array", "items": {"type": "string"}}, "fix_notes": {"type": "array", "items": {"type": "string"}},
    "severity": {"type": "string", "enum": ["minor", "major"]}}}
# A block's severity decides whether the video may still post as the tick's last resort (operator, 9 Nov); a block with
# none counts as major.
SEVERITY = ('If you block, set severity to "major" when any reason is about the message (unclear or misleading), safety, '
            "the banned list, an expert persona or clinical wording, platform rules, a title or cover promise the video "
            "doesn't keep, or a video that can't be watched (no sound, a blank or broken picture). Otherwise set it to "
            '"minor".')  # a channel's own review playbook names what a broken picture is for it
STRIPS = ("{names} show one frame a second, in order, 5 to a row: the first half of the film, then the second. A solid "
          "magenta tile is padding after the last frame, not a frame.")
LEFT = ("## The code's checks still found\nThese are what the pre-render check and the render's visual checks still "
        "found after their one fix round. Judge each one against the pictures: block for it only if you can see it. A frame named beside a "
        "finding is a full-size picture from the middle of its time span: look at it first.")
# A re-review after a repair re-checks the earlier blockers and what the repair broke (9 Nov: each round had blocked on
# something new, and only one repair is allowed).
RE_REVIEW = ("Its fix notes were applied. Block only if one of those reasons is still true, or if the repair broke "
             "something the last version had right. Anything else you notice goes in defects, not reasons.")
VERDICT_TRIES = 2  # an invalid verdict gets one deterministic retry (5.5.5), never a pass


def _verdict(job: Job, ws: dict, claude_bin, sensor, evidence, prior: dict | None = None) -> tuple[dict | None, float]:
    work = job.dir / "review"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    shutil.copyfile(job.dir / "sheet.png", work / "sheet.png")  # its folder holds the evidence and nothing else
    files, notes = ["sheet.png"], []
    strip = sorted(p.name for p in job.dir.glob("strip-*.png"))  # one frame a second (engine Task 6)
    for name in strip:
        shutil.copyfile(job.dir / name, work / name)
    files += strip
    left_file = job.dir / "render_left.json"
    left = json.loads(left_file.read_text(encoding="utf-8")) if left_file.exists() else {}
    left = [f for k in ("check_left", "visual_left") for f in left.get(k) or []]
    for f in left:  # each timed finding's own full-size frame (ruling E12), beside the strips
        if f.get("frame") and (job.dir / f["frame"]).is_file():
            shutil.copyfile(job.dir / f["frame"], work / f["frame"])
            files.append(f["frame"])
        else:
            f.pop("frame", None)
    unframed = sum("frame" not in f for f in left)
    left = [f"- {f.get('code', '?')}: {f.get('message', f)}" + (f" (frame: {f['frame']})" if "frame" in f else "")
            for f in left] + ([f"{unframed} of these have no frame of their own (no time span, or past the "
                               f"{LEFT_FRAMES}-frame cap): judge those from the strips."] if unframed else [])
    if evidence is not None:  # motion and sound, as pictures and numbers (factory/evidence.py)
        ev = evidence(job, job.dir / "evidence")
        for src in ev["files"]:
            shutil.copyfile(src, work / Path(src).name)
            files.append(Path(src).name)
        notes = ["## Motion and sound measurements", json.dumps(ev["summary"], indent=2)]
    script = json.loads((job.dir / "script.json").read_text(encoding="utf-8"))
    meta = json.loads((job.dir / "metadata.json").read_text(encoding="utf-8"))
    again = ["## Re-review after a repair", "The last review blocked this video for:\n" + "\n".join(
        f"{i}. {r}" for i, r in enumerate(prior["reasons"], 1)), RE_REVIEW] if prior else []
    prompt = "\n\n".join([f"Review this {ws['display_name']} {format_of(ws)['label']}. Follow the review playbook "
                          "in your system prompt. "
                          f"Read every evidence file in your folder: {', '.join(files)}.",
                          *([STRIPS.format(names=" and ".join(strip))] if strip else []), SEVERITY,
                          *([LEFT + "\n" + "\n".join(left)] if left else []), *again, *notes,
                          "## Narration", script["narration"], "## Metadata", json.dumps(meta, indent=2)])
    cost = 0.0
    for _ in range(VERDICT_TRIES):
        r = call("review", prompt, cwd=work, tools=("Read",), schema=SCHEMA,
                 system_file=Path(ws["dir"]) / ws["playbooks"]["review"], claude_bin=claude_bin,
                 usage_log=job.dir / "usage.jsonl", sensor=sensor)
        cost += r.cost_usd or 0.0
        if r.isolation:
            raise RuntimeError(f"review run not isolated, output unused: {r.isolation}")
        if r.data is not None:
            return r.data, cost
    return None, cost


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _finish_repair(job: Job, pending: Path, p: dict, repair, regate) -> None:
    """Phase "repair" is the code run from the notes; phase "gate" re-runs the check, the collision gate and the render
    on what it left. The file is removed only after the repaired render returns."""
    if repair is None or regate is None:
        raise RuntimeError("a repair is pending, and this review has no repair and gates to finish it with")
    _save(pending, p)
    if p["phase"] == "repair":
        try:
            repair(job, p["verdict"])
        except QuotaWait:
            raise  # the writer never ran (or was cut off): the same repair runs again, on the regeneration it holds
        except BaseException:
            _save(pending, {**p, "phase": "gate"})  # the writer may have changed files: their gates come first
            raise
        p = {**p, "phase": "gate"}
        _save(pending, p)
    regate(job)
    pending.unlink()


def _repair_round(job: Job, verdict: dict, round_no: int, pending: Path, repair, regate) -> None:
    """A block at round 1 gets the one repair round, if the budget has one; any later block quarantines."""
    if round_no >= 2:
        raise Quarantined(f"the review blocked again after the repair round: {verdict['reasons'][:3]}")
    if repair is None or regate is None or not job.spend_regeneration():
        raise Quarantined(f"the review blocked with no repair left: {verdict['reasons'][:3]}")
    _finish_repair(job, pending, {"round": round_no, "verdict": verdict, "phase": "repair"}, repair, regate)


def review(job: Job, *, repair: Callable[[Job, dict], object] | None = None,
           regate: Callable[[Job], object] | None = None, evidence: Callable[[Job, Path], dict] | None = None,
           claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    """Pass, or block -> one repair round (if the budget allows) -> its gates -> review again. repair(job, verdict) is
    the code run from the notes; regate(job) re-runs the check, collision gate and render on what it left. Writes
    review.json each round and appends to review_history.json. evidence(job, folder) -> {"files": [...],
    "summary": {...}} adds motion and sound evidence for each round."""
    ws, cost = load(job.workspace), 0.0
    hist, pending = job.dir / "review_history.json", job.dir / "repair_pending.json"
    history = json.loads(hist.read_text(encoding="utf-8")) if hist.exists() else []
    first = 1
    if pending.exists():  # a repair began and its render never returned: finish it before any verdict, at round 2
        _finish_repair(job, pending, json.loads(pending.read_text(encoding="utf-8")), repair, regate)
        first = 2
    elif history and history[-1] and history[-1]["block"]:
        # a recorded block with no repair under way (a job from before repair_pending.json, or a stop between the
        # verdict and that file): the owed repair runs from it, never a fresh verdict on the blocked render.
        # ponytail: a stop between the spend and the file pays twice; mark the spend in the file if that ever bites
        cycle = job.dir / "review_cycle.json"  # a budget reset's fresh cycle: rounds count from there
        start = json.loads(cycle.read_text(encoding="utf-8"))["start"] if cycle.exists() else 0
        _repair_round(job, history[-1], max(1, len(history) - start), pending, repair, regate)
        first = 2
    for round_no in range(first, 3):
        prior = history[-1] if round_no >= 2 and history and history[-1] and history[-1].get("block") else None
        verdict, spent = _verdict(job, ws, claude_bin, sensor, evidence, prior)
        cost += spent
        history.append(verdict)
        hist.write_text(json.dumps(history, indent=2), encoding="utf-8")
        if verdict is None:
            raise Quarantined(f"no valid review verdict after {VERDICT_TRIES} tries; a missing review never passes")
        if verdict["block"] and verdict.get("severity") == "minor":  # operator, 11 Nov: minor flaws post; no repair is
            # paid for them. The reasons stay as defects (the learning loop reads them) and as "waived".
            verdict = {**verdict, "block": False, "waived": verdict["reasons"],
                       "defects": verdict["defects"] + verdict["reasons"]}
        (job.dir / "review.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        if not verdict["block"]:
            return {"block": False, "rounds": round_no, "defects": verdict["defects"], "cost_usd": round(cost, 4)}
        _repair_round(job, verdict, round_no, pending, repair, regate)
    raise AssertionError("unreachable")
