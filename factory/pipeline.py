"""The explainer pipeline for one Short, brief to upload (design 5.5.2; Plan 2b Task 12). Every stage
checkpoints, so `job run` resumes where a job stopped; `until` stops after a named stage (one heavy job at a time:
the dry run can do its API-bound stages while something else renders, and render later)."""
from __future__ import annotations

import json
from pathlib import Path

from .assets import harvest
from .audio import declared_events
from .claude_call import SENSOR
from .evidence import motion, sound
from .jobs import Job, Stage, run
from .jobs import load as load_job
from .publish import upload_private
from . import lessons, library
from .clip.pipeline import clip_cut
from .prepublic import prepublic
from .stages.fetch_sources import fetch_sources
from .stages.metadata import metadata
from .stages.music import music
from .stages.narrate import narrate
from .stages.render_stage import render_stage
from .stages.research import research
from .stages.review import review
from .stages.scene import fix, recheck, scene
from .stages.script import script
from .stages.shots import shots
from .stages.story import story
from .stages.story_check import story_check
from .stages.story_scene import story_scene
from .workspace import format_of
from .workspace import load as load_ws


def _evidence(job: Job, folder: Path) -> dict:
    """Motion (motiscope) and sound (earshot) evidence for the review, from the final render."""
    final = job.dir / "final.mp4"
    cues = [t for _, t in declared_events(job.dir / "project" / "index.html")]
    m = motion(final, folder / "motion")
    s = sound(final, folder / "sound", cues)
    return {"files": [m["png"], s["png"]], "summary": {"motion": m["summary"], "sound_critique": s["critique"]}}


def _review(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    def repair(j: Job, verdict: dict) -> None:  # the repair round's code run, from the review's notes
        fix(j, {"fix_notes": verdict["fix_notes"], "defects": verdict["defects"]}, playbook="repair", check=False,
            claude_bin=claude_bin, sensor=sensor)

    def regate(j: Job) -> None:  # its gates: the check (fix rounds from the budget), the collision gate, the render
        recheck(j, claude_bin=claude_bin, sensor=sensor)
        render_stage(j, claude_bin=claude_bin, sensor=sensor)
    result = review(job, repair=repair, regate=regate, evidence=_evidence, claude_bin=claude_bin, sensor=sensor)
    ws = load_ws(job.workspace)
    if format_of(ws)["pipeline"] == "story":  # a passing reel's new assets join the library (owner, 10 Nov)
        try:
            saved = library.save(job, ws)
        except Exception as e:  # noqa: BLE001 - a busy git (index.lock) never fails a passed review (final review D)
            saved = {"error": str(e)}
            library._record(job, [], [], error=str(e))
        return {**(result or {}), "library": saved}
    # a passing video's new sets and props join the channel library (operator, 6 Nov: cheaper with every run)
    return {**(result or {}), "assets": harvest(job.dir / "project", ws, job.id)}


def _publish(job: Job) -> dict:
    payload = json.loads((job.dir / "upload_payload.json").read_text(encoding="utf-8"))
    return upload_private(job, job.dir / "final.mp4", payload)


STAGES: list[Stage] = [
    ("research", research, "creative"), ("fetch_sources", fetch_sources, "deterministic"),
    ("script", script, "creative"), ("narrate", narrate, "deterministic"), ("scene", scene, "creative"),
    ("music", music, "creative"), ("render", render_stage, "deterministic"), ("metadata", metadata, "creative"),
    ("review", _review, "creative"), ("prepublic", prepublic, "deterministic"), ("publish", _publish, "deterministic"),
]


# The story format (story plan Tasks 2 and 6; 8.5 item 20): no research or sources, since fiction quotes none. The
# film is staged on the channel's kit (story_scene), then music, render, metadata, review and publish as for any Short.
STORY_STAGES: list[Stage] = [
    ("story", story, "creative"), ("narrate", narrate, "deterministic"),
    ("story_check", story_check, "deterministic"), ("shots", shots, "creative"),
    ("story_scene", story_scene, "creative"), ("music", music, "creative"), ("render", render_stage, "deterministic"),
    ("metadata", metadata, "creative"),
    ("review", _review, "creative"), ("prepublic", prepublic, "deterministic"), ("publish", _publish, "deterministic"),
]
CLIP_STAGES: list[Stage] = [
    ("clip_cut", clip_cut, "deterministic"),
    ("metadata", metadata, "creative"),
    ("prepublic", prepublic, "deterministic"),
    ("publish", _publish, "deterministic"),
]
PIPELINES = {"explainer": STAGES, "story": STORY_STAGES, "clip": CLIP_STAGES}


def run_job(job_id: str, until: str | None = None) -> str:
    stages_all = PIPELINES[format_of(load_ws(load_job(job_id).workspace))["pipeline"]]
    names = [s[0] for s in stages_all]
    if until is not None and until not in names:
        raise ValueError(f"unknown stage {until!r}; stages: {names}")
    stages = stages_all if until is None else stages_all[: names.index(until) + 1]
    if (load_job(job_id).dir / lessons.PENDING).exists():  # a retry the tick started: the lesson comes first (9 Nov)
        stages = [("lessons", lessons.stage, "creative"), *stages]
    state = run(job_id, stages)
    if until == "prepublic" and state == "done":
        try:
            from . import scheduler
            scheduler.chain_next(job_id, root=load_job(job_id).root)
        except Exception:
            pass
    return state
