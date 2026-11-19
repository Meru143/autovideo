"""scene_code with fix rounds: Claude writes the HyperFrames composition from the script beats and timing.json on the
brand kit; the renderer's check (lint, runtime, the layout pass at every tween boundary, motion, contrast) gates it,
and its findings go back to a fix run from the shared regeneration budget (design 5.5.2 steps 10-11, 6a; Plan 2b
Task 7; FAILURE_MODES.md, scene)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..claude_call import SENSOR, call
from ..collide import gate as collision_gate
from ..jobs import Job
from ..render import CheckFailed, check, install_vendor
from ..workspace import format_of, load
from .. import ROOT

GUIDE = ROOT / "factory" / "scene_guide"  # the production authoring guide: the bake-off's, minus its capture probe (5 Nov)
CODE_TOOLS = ("Read", "Write", "Edit")
CHECK_FIXES_FREE = 3  # a check loop's own fix rounds for mechanical findings, before the shared budget (6 Nov)


def _system(ws: dict, stage: str, out: Path) -> Path:
    """The hyperframes-authoring guide, the scene playbook (its rules and contracts bind every code run, and a fix or
    repair writer can't read files outside its folder) and, for fix and repair, that stage's playbook."""
    books = ["scene"] + ([stage] if stage != "scene" else [])
    out.write_text("\n\n---\n\n".join([(GUIDE / "SKILL.md").read_text(encoding="utf-8")]
                                      + [(Path(ws["dir"]) / ws["playbooks"][b]).read_text(encoding="utf-8") for b in books]),
                   encoding="utf-8")
    return out


def _guide(job: Job) -> Path:
    """A fresh copy of the authoring guide in the job for each code run (final review, Important 4): the writer reads
    its references there and can change only its own copy, never the guide every later run takes as its system prompt.
    The copy also records which guide the job used."""
    copy = job.dir / "guide"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(GUIDE, copy)
    return copy


def _kit(ws: dict) -> str:
    """What the channel's own brand kit holds, for the prompt: files, and folders with a trailing slash."""
    kit = Path(ws["dir"]) / "brand"
    return ", ".join(sorted(f"{p.name}/" if p.is_dir() else p.name for p in kit.iterdir()
                            if p.suffix != ".py" and p.name != "__pycache__")) if kit.is_dir() else "empty"


def _brand(ws: dict, project: Path) -> None:
    kit = Path(ws["dir"]) / "brand"
    shutil.copytree(kit, project / "brand", dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    install_vendor(project)


def fix(job: Job, notes: list, *, playbook: str = "fix", spend: str | None = None, check: bool = True,
        claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    """One code run on the existing composition from findings (fix) or a review's notes (repair), then the check loop
    again (check=False: the caller runs the gates later, through recheck). spend: this run pays one regeneration
    (Quarantined with this message when none is left), given back if it hits a quota wall."""
    ws, project, work = load(job.workspace), job.dir / "project", job.dir / "scene"
    work.mkdir(exist_ok=True)  # the render stage and the review call this too, not only the scene stage
    what = "the review's fix notes and defects" if playbook == "repair" else "these findings"
    with job.regeneration(spend):
        r = call(playbook, f"Fix the composition in your folder from {what}, with the smallest changes that clear "
                           "them, keeping the shot plan and the brand rules.\n\n## Notes\n" + json.dumps(notes, indent=2),
                 cwd=project, tools=CODE_TOOLS, add_dirs=(_guide(job),),
                 system_file=_system(ws, playbook, work / f"system_{playbook}.md"), claude_bin=claude_bin,
                 usage_log=job.dir / "usage.jsonl", sensor=sensor)
    if r.isolation:
        raise RuntimeError(f"{playbook} run not isolated, output unused: {r.isolation}")
    return _check_loop(job, r, claude_bin=claude_bin, sensor=sensor) if check else {"runs": 1, "cost_usd": r.cost_usd}


def recheck(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    """The check loop on the composition as it stands: a repair's gates, which run before any new verdict."""
    return _check_loop(job, None, claude_bin=claude_bin, sensor=sensor)


def _check_loop(job: Job, r, *, claude_bin, sensor) -> dict:
    """Check the composition; each failure goes to a fix run from the shared budget until it passes. r is the run that
    just wrote it, or None when the gates re-run on what is already there."""
    ws, project, work = load(job.workspace), job.dir / "project", job.dir / "scene"
    work.mkdir(exist_ok=True)  # recheck can run before any scene call made it
    calls, cost, free = [], 0.0, CHECK_FIXES_FREE
    while True:
        if r is not None:
            cost += r.cost_usd or 0.0
            calls.append({"stage": r.stage, "cost_usd": r.cost_usd, "seconds": r.seconds, "timed_out": r.timed_out})
            if r.isolation:
                raise RuntimeError(f"{r.stage} run not isolated, output unused: {r.isolation}")
        if missing := [n for n in ("index.html", "shot_plan.json") if not (project / n).is_file()]:
            raise RuntimeError(f"the composition has no {missing}" + (
                f" after {r.stage} (exit {r.exit_code}, timed out {r.timed_out})" if r is not None else ""))
        try:
            check(project, aspect=format_of(ws)["aspect"])
            if format_of(ws)["pipeline"] != "story":  # a story's film is a canvas: no page element is a drawing
                collision_gate(project)  # drawings against drawings, which the renderer's check doesn't cover
            break
        except CheckFailed as e:
            findings = [{k: f.get(k) for k in ("code", "severity", "category", "message", "selector", "time", "text", "fixHint",
                                                 "containerSelector", "overflow", "fg", "bg", "suggestedColor")
                         if f.get(k) is not None} for f in e.findings]
            (work / f"findings_{len(calls)}.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
            free -= 1  # the loop's own rounds first; then the shared budget (FAILURE_MODES.md, scene 2)
            with job.regeneration(None if free >= 0 else f"check still failing after the regeneration budget: "
                                                         f"{sorted({f.get('code') for f in findings})}"):
                r = call("fix", "The renderer's check failed on the composition in your folder. Fix every finding "
                                "with the smallest change that clears it, keeping the shot plan and the brand rules."
                                "\n\n## Findings\n" + json.dumps(findings, indent=2), cwd=project, tools=CODE_TOOLS,
                         add_dirs=(_guide(job),), system_file=_system(ws, "fix", work / "system_fix.md"),
                         claude_bin=claude_bin, usage_log=job.dir / "usage.jsonl", sensor=sensor)
    log = work / "calls.json"
    previous = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
    log.write_text(json.dumps(previous + calls, indent=2), encoding="utf-8")
    return {"runs": len(calls), "fix_rounds": max(len(calls) - 1, 0), "cost_usd": round(cost, 4)}


def scene(job: Job, *, claude_bin: list[str] | None = None, sensor: Path = SENSOR) -> dict:
    ws = load(job.workspace)
    project, work = job.dir / "project", job.dir / "scene"
    project.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    _brand(ws, project)
    written, marker = (lambda: all((project / n).is_file() for n in ("index.html", "shot_plan.json"))), work / "writer.done.json"
    if marker.exists() and written():  # a stop after the writer: resume at the check loop, never pay for it twice
        return _check_loop(job, None, claude_bin=claude_bin, sensor=sensor)
    script = json.loads((job.dir / "script.json").read_text(encoding="utf-8"))
    timing = json.loads((job.dir / "timing.json").read_text(encoding="utf-8"))
    prompt = "\n\n".join([
        f"Write this {ws['display_name']} {format_of(ws)['label']}'s composition into your current folder: "
        "index.html and shot_plan.json. Follow the hyperframes-authoring guide and the scene playbook in your system "
        f"prompt. The brand kit is in ./brand/ ({_kit(ws)}); GSAP is ./vendor/gsap.min.js. Link both with relative "
        "paths.",
        "## Brief", job.brief.read_text(encoding="utf-8").strip(),
        "## Script (beats with the source each claim rests on, and the narration)", json.dumps(
            {"beats": [{**b, "claims": [{k: v for k, v in c.items() if k != "quote"} for c in b.get("claims", [])]}
                       for b in script["beats"]],  # a claim's verbatim quote is web text: never for a writer with tools
             "narration": script["narration"], "added_value": script.get("added_value")}, indent=2),
        f"## Timing (seconds). The composition runs audio_duration_s ({timing['audio_duration_s']}) plus a closing "
        f"hold of up to {format_of(ws)['max_hold_s']:g} s.", json.dumps(timing, indent=2)])
    r = call("scene", prompt, cwd=project, tools=CODE_TOOLS, add_dirs=(_guide(job),),
             system_file=_system(ws, "scene", work / "system_scene.md"), claude_bin=claude_bin,
             usage_log=job.dir / "usage.jsonl", sensor=sensor)
    if not r.isolation and written():  # FAILURE_MODES.md, scene 9: the writer's work survives any later stop
        marker.write_text(json.dumps({"seconds": r.seconds, "cost_usd": r.cost_usd, "exit": r.exit_code,
                                      "timed_out": r.timed_out}), encoding="utf-8")
    return _check_loop(job, r, claude_bin=claude_bin, sensor=sensor)
