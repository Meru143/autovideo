"""python -m factory <command>  (run from the repo root with .venv)"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import selftest
from .workspace import channels


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # tool output carries symbols a cp1252 pipe can't encode
        stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="factory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("selftest", help="E2E selftest; writes factory/results/e2e/<stamp>-selftest.json")
    st.add_argument("--stages", default=None, help="comma-separated stage names (default: all)")
    sub.add_parser("lint", help="AI-sounding writing in a script; exit 1 on any finding").add_argument("script", type=Path)
    sub.add_parser("still", help="still spans over 1.5 s (closing hold up to 3 s); exit 1 on any").add_argument(
        "video", type=Path)
    vo = sub.add_parser("voice", help="narrate a script for a brand and check it (one Gemini request for explainers)")
    vo.add_argument("brand", choices=channels())  # every channel folder with a settings.json
    vo.add_argument("script", type=Path)
    vo.add_argument("--out", type=Path, required=True)
    jb = sub.add_parser("job", help="create or inspect a job (job run arrives with the pipeline, Plan 2b Task 12)")
    jsub = jb.add_subparsers(dest="job_cmd", required=True)
    jn = jsub.add_parser("new", help="start a job from a brief; prints its ID")
    jn.add_argument("workspace")
    jn.add_argument("brief", type=Path)
    jsub.add_parser("show", help="print a job's state and finished stages").add_argument("job_id")
    jr = jsub.add_parser("run", help="run a job's unfinished stages in order (resumes after a stop)")
    jr.add_argument("job_id")
    jr.add_argument("--until", default=None, help="stop after this stage (e.g. scene: render later)")
    js = jsub.add_parser("reset", help="reopen a quarantined (or any) job, logged in its operator.jsonl")
    js.add_argument("job_id")
    js.add_argument("--budget", action="store_true", help="also restore the regeneration budget")
    sub.add_parser("loop", help="the learning loop's weekly step for one channel (Plan 3b)").add_argument("channel")
    sub.add_parser("tick", help="one scheduler tick: publish at a due slot, one job, topics (Plan 3a Task 6); Task "
                               "Scheduler runs it every 15 minutes")
    sub.add_parser("backlog", help="briefs, finished videos and next slots per channel").add_subparsers(
        dest="backlog_cmd", required=True).add_parser("show", help="print the backlog")
    kit_sub = sub.add_parser("kit", help="a story channel's kit (story plan Tasks 3-5)").add_subparsers(
        dest="kit_cmd", required=True)
    kt = kit_sub.add_parser("inventory", help="what the given jobs' shot tables use: writes brand/inventory.json and "
                                              "prints the short list for the operator's OK")
    kt.add_argument("workspace")
    kt.add_argument("--jobs", nargs="+", required=True, help="the approved stories' job ids")
    kb = kit_sub.add_parser("build", help="write, check and install brand/cast.js or sets.js for the inventory (a "
                                          "writer run; the test key while building), then print where its stills are "
                                          "for the operator's look approval")
    kb.add_argument("workspace")
    kb.add_argument("kind", choices=("cast", "sets"))
    cs = sub.add_parser("console", help="the operator console: a local page for the factory (127.0.0.1 only); prints "
                                        "its address and opens it")
    cs.add_argument("--port", type=int, default=0, help="default 0: the OS picks a free port")
    cs.add_argument("--no-browser", action="store_true")
    cs.add_argument("--fixture", type=Path, default=None, help="serve a scratch factory built in this folder, with "
                                                               "fake publish legs (for tests)")
    cs.add_argument("--key", default=None, help="the launch key, with --fixture only (else a new one every launch)")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest.run(a.stages.split(",") if a.stages else None, ["python", "-m", "factory", *sys.argv[1:]])
    if a.cmd == "console":
        import os
        import re
        import secrets

        from . import OUT, ROOT
        from .console import server
        if a.key is not None and (a.fixture is None or not re.fullmatch(r"[A-Za-z0-9_-]+", a.key)):
            ap.error("--key goes with --fixture only, and takes letters, digits, - and _")
        root, repo, legs, key = OUT, ROOT, None, a.key or secrets.token_urlsafe(24)
        if a.fixture:
            from .console import fixture
            f = fixture.build(a.fixture)
            os.environ["FACTORY_WORKSPACES"], os.environ["FACTORY_CLAUDE_CONFIG"] = (
                str(f["channels_root"]), str(f["claude_config"]))
            os.environ["FACTORY_SETTINGS"] = str(f["settings"])  # saves commit in the scratch repo, never the live file
            root, repo, legs = f["root"], f["repo"], fixture.legs(f["root"])
        try:
            server.serve(root, repo=repo, key=key, port=a.port, legs=legs, open_browser=not a.no_browser)
        except OSError as e:  # a taken port: say so, rather than a traceback
            print(f"console: cannot listen on port {a.port}: {e}")
            return 1
        return 0
    if a.cmd == "lint":
        from .script_lint import lint
        found = lint(a.script.read_text(encoding="utf-8"))
        for f in found:
            print(f"{f['rule']}: {f['evidence']}")
        return 1 if found else 0
    if a.cmd == "still":
        from .stillness import check
        found = check(a.video)
        for f in found:
            print(f"{f['start']}-{f['end']} s ({f['seconds']} s): {f['why']}")
        return 1 if found else 0
    if a.cmd == "voice":
        from .voice import synthesize
        try:
            r = synthesize(a.brand, a.script.read_text(encoding="utf-8"), a.out)
        except RuntimeError as e:  # VoiceCheckFailed and QuotaExhausted included; voice.json keeps the numbers
            print(f"voice {a.brand}: {type(e).__name__}: {e}")
            return 1
        print(f"voice {a.brand}: {r['seconds']} s, WER {r['wer']:.1%}, {r['rate']} {r['rate_unit']} -> {a.out}")
        return 0
    if a.cmd == "job":
        import json

        from . import jobs
        from .workspace import load
        if a.job_cmd == "new":
            load(a.workspace)  # refuse a broken workspace before a job exists
            print(jobs.new(a.workspace, a.brief))
            return 0
        if a.job_cmd == "reset":
            print(f"job {a.job_id}: {jobs.reset(a.job_id, budget=a.budget)}")
            return 0
        if a.job_cmd == "run":
            from .pipeline import run_job
            state = run_job(a.job_id, a.until)
            print(f"job {a.job_id}: {state}")
            return 0 if state in ("done", "uploaded_private") else 1
        print(json.dumps(jobs.show(a.job_id), indent=2, default=str))
        return 0
    if a.cmd in ("tick", "backlog"):
        import json

        from . import scheduler
        report = scheduler.backlog() if a.cmd == "backlog" else scheduler.tick()
        if a.cmd == "tick" and not report.get("busy"):
            from . import stats  # each public video's numbers, once a day per channel (9 Nov); never breaks the tick
            try:
                if pulled := stats.daily():
                    report["stats"] = pulled
            except Exception as e:  # the tick already ran; the report says why the pull didn't
                report["stats"] = f"{type(e).__name__}: {str(e)[:200]}"
            from .loop.apply import weekly  # the learning loop's step, once a week per channel, in its own process
            try:
                if started := weekly():
                    report["loop"] = started
            except Exception as e:
                report["loop"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(json.dumps(report, indent=2, default=str))
        return 0
    if a.cmd == "loop":
        import json
        from datetime import datetime, timezone

        from .loop.apply import step
        from .topics import store
        from .workspace import load
        conn = store.connect()
        try:
            print(json.dumps(step(conn, load(a.channel, need=()), datetime.now(timezone.utc)), indent=2, default=str))
        finally:
            conn.close()
        return 0
    if a.cmd == "kit":
        import json
        from pathlib import Path as P

        from . import OUT
        from .kit import inventory, summary
        from .workspace import load
        ws = load(a.workspace)
        if a.kit_cmd == "build":
            from .kit_build import build
            r = build(ws, a.kind)
            print(json.dumps({k: v for k, v in r.items() if k != "findings"} | {"rounds_findings": r["findings"]},
                             indent=2, default=str))
            return 0
        tables = [json.loads((OUT / "jobs" / j / "shot_table.json").read_text(encoding="utf-8")) for j in a.jobs]
        inv = {"jobs": a.jobs, **inventory([t["shots"] if isinstance(t, dict) else t for t in tables])}
        out = P(ws["dir"]) / "brand" / "inventory.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(inv, indent=2), encoding="utf-8")
        print(f"{a.workspace} kit needs: {summary(inv)} -> {out}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
